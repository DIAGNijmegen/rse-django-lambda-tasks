# Bugfix Requirements Document

## Introduction

This document captures seven issues identified in the existing `django-lambda-tasks` implementation. The issues span the public API ergonomics, settings design, timeout safety, a new eager-execution mode for development/testing, and code-quality requirements (kwargs-only signatures and strict typing). Together they tighten the library's contract, remove unnecessary configuration surface, and make local development easier.

---

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a developer applies `@lambda_task` to a function, THEN the system requires an explicit import of that task module inside `AppConfig.ready()` for the registry to be populated before the Lambda handler processes messages.

1.2 WHEN `lambda_tasks/conf.py` is read, THEN the system exposes a `LAMBDA_TASKS_DEFAULT_DELAY` setting that adds unnecessary configuration surface with no meaningful benefit over a hardcoded default of `0`.

1.3 WHEN the settings/configuration module is referenced, THEN the system uses the filename `conf.py`, which does not match the Django convention of naming app-level settings helpers `settings.py`.

1.4 WHEN a `soft_timeout` or `hard_timeout` value greater than 900 seconds is supplied (at decoration time, at `on_commit` time, or via global settings), THEN the system accepts the value without error, allowing timeouts that exceed the AWS Lambda maximum runtime of 15 minutes.

1.5 WHEN `LAMBDA_TASKS_EAGER` is absent from Django settings, THEN the system has no mechanism for synchronous in-process task execution, forcing developers to mock SQS or spin up real infrastructure during local development and testing.

1.6 WHEN any function or method in the library is called, THEN the system permits positional arguments at the call site for internal and public API functions, making call signatures ambiguous and inconsistent with the kwargs-only contract imposed on task functions themselves.

1.7 WHEN the codebase is analysed with `ty` in strict mode, THEN the system produces type errors because type annotations are incomplete or incorrect across the library modules.

---

### Expected Behavior (Correct)

2.1 WHEN a developer applies `@lambda_task` to a function, THEN the system SHALL automatically register the task in the registry at decoration time, with no requirement to import the module in `AppConfig.ready()` or anywhere else.

2.2 WHEN `lambda_tasks/settings.py` is read, THEN the system SHALL NOT expose a `LAMBDA_TASKS_DEFAULT_DELAY` setting; the delay default SHALL be `0` and SHALL be hardcoded internally.

2.3 WHEN the settings/configuration module is referenced, THEN the system SHALL use the filename `settings.py` (i.e. `lambda_tasks/settings.py`) in place of `conf.py`.

2.4 WHEN a `soft_timeout` or `hard_timeout` value greater than 900 seconds is supplied at any configuration point (decorator, `on_commit`, or global settings), THEN the system SHALL raise a `ConfigurationError` and SHALL NOT proceed with that configuration.

2.5 WHEN `LAMBDA_TASKS_EAGER = True` is present in Django settings and `.on_commit(**kwargs)` is called, THEN the system SHALL execute the task synchronously and immediately in-process instead of sending a message to SQS.

2.6 WHEN any function or method in the library is defined, THEN the system SHALL use keyword-only arguments for every parameter (no positional parameters in any public or internal function/method signature).

2.7 WHEN the codebase is analysed with `ty` in strict mode, THEN the system SHALL produce zero type errors; all functions, methods, and variables SHALL carry complete and correct type annotations.

---

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `@lambda_task` is applied to a function with positional parameters, THEN the system SHALL CONTINUE TO raise `TypeError` at decoration time.

3.2 WHEN `soft_timeout >= hard_timeout` is supplied at decoration time or at `on_commit` time, THEN the system SHALL CONTINUE TO raise `ConfigurationError`.

3.3 WHEN `on_commit` is called inside an active database transaction, THEN the system SHALL CONTINUE TO defer SQS dispatch until after the transaction commits.

3.4 WHEN `on_commit` is called outside an active database transaction, THEN the system SHALL CONTINUE TO dispatch the SQS message immediately.

3.5 WHEN `LAMBDA_TASKS_EAGER` is absent or `False`, THEN the system SHALL CONTINUE TO send SQS messages via boto3 on `on_commit` as before.

3.6 WHEN a valid `soft_timeout` and `hard_timeout` pair (both ≤ 900 seconds, soft < hard) is supplied, THEN the system SHALL CONTINUE TO enforce those timeouts via `SIGALRM` during task execution.

3.7 WHEN `LAMBDA_TASKS_QUEUES` or `LAMBDA_TASKS_SQS_QUEUE_URL` is absent from settings, THEN the system SHALL CONTINUE TO raise `ImproperlyConfigured` on first use.

3.8 WHEN a task completes successfully, THEN the system SHALL CONTINUE TO update the `TaskRecord` with status `SUCCESS`, the return value, and the end time.

3.9 WHEN a task raises an unhandled exception, THEN the system SHALL CONTINUE TO roll back the atomic block and write a `FAILED` `TaskRecord` outside it.

3.10 WHEN the Lambda handler receives a batch of SQS records, THEN the system SHALL CONTINUE TO process each record independently and return partial-batch failure information.

---

## Bug Condition Pseudocode

### Bug 1 — Awkward task module imports in `ready()`

```pascal
FUNCTION isBugCondition_1(X)
  INPUT: X of type TaskRegistrationContext
  OUTPUT: boolean

  RETURN X.task_module_imported_in_ready = true
         AND X.registry_populated_without_explicit_import = false
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_1(X) DO
  result ← applyDecorator'(X)
  ASSERT registry.get(X.task_name) IS NOT NULL
         AND no_import_in_ready_required(result)
END FOR

// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition_1(X) DO
  ASSERT applyDecorator(X) = applyDecorator'(X)
END FOR
```

---

### Bug 2 — Unnecessary `LAMBDA_TASKS_DEFAULT_DELAY` setting

```pascal
FUNCTION isBugCondition_2(X)
  INPUT: X of type SettingsObject
  OUTPUT: boolean

  RETURN "LAMBDA_TASKS_DEFAULT_DELAY" IN X.defined_settings
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_2(X) DO
  result ← loadSettings'(X)
  ASSERT "DEFAULT_DELAY" NOT IN result.exposed_attributes
END FOR

// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition_2(X) DO
  ASSERT loadSettings(X).queue_resolution = loadSettings'(X).queue_resolution
  AND    loadSettings(X).timeout_resolution = loadSettings'(X).timeout_resolution
END FOR
```

---

### Bug 3 — `conf.py` should be `settings.py`

```pascal
FUNCTION isBugCondition_3(X)
  INPUT: X of type ModuleReference
  OUTPUT: boolean

  RETURN X.filename = "conf.py"
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_3(X) DO
  result ← resolveModule'(X)
  ASSERT result.filename = "settings.py"
         AND result.public_api EQUIVALENT_TO original_conf_public_api
END FOR
```

---

### Bug 4 — Timeouts must be capped at 900 seconds

```pascal
FUNCTION isBugCondition_4(X)
  INPUT: X of type TimeoutValue
  OUTPUT: boolean

  RETURN X.value > 900
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_4(X) DO
  result ← configureTimeout'(X)
  ASSERT raises ConfigurationError(result)
END FOR

// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition_4(X) DO
  ASSERT configureTimeout(X) = configureTimeout'(X)
END FOR
```

---

### Bug 5 — No eager execution mode

```pascal
FUNCTION isBugCondition_5(X)
  INPUT: X of type OnCommitInvocation
  OUTPUT: boolean

  RETURN X.settings.LAMBDA_TASKS_EAGER = true
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_5(X) DO
  result ← onCommit'(X)
  ASSERT task_executed_synchronously(result)
         AND no_sqs_message_sent(result)
END FOR

// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition_5(X) DO
  ASSERT onCommit(X) = onCommit'(X)   // SQS path unchanged
END FOR
```

---

### Bug 6 — Functions/methods accept positional arguments

```pascal
FUNCTION isBugCondition_6(X)
  INPUT: X of type FunctionSignature
  OUTPUT: boolean

  RETURN EXISTS param IN X.parameters WHERE param.kind IN {POSITIONAL_ONLY, POSITIONAL_OR_KEYWORD}
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_6(X) DO
  result ← inspectSignature'(X)
  ASSERT ALL params IN result.parameters SATISFY param.kind = KEYWORD_ONLY
END FOR
```

---

### Bug 7 — Incomplete type annotations

```pascal
FUNCTION isBugCondition_7(X)
  INPUT: X of type SourceFile
  OUTPUT: boolean

  RETURN ty_strict_check(X).error_count > 0
END FUNCTION

// Property: Fix Checking
FOR ALL X WHERE isBugCondition_7(X) DO
  result ← ty_strict_check'(X)
  ASSERT result.error_count = 0
END FOR
```
