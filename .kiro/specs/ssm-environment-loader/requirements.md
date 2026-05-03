# Requirements Document

## Introduction

This feature adds SSM Parameter Store environment loading to the Lambda cold-start sequence. When the environment variable `LAMBDA_TASKS_SSM_ENVIRONMENT` is set, the Lambda handler reads the named SSM parameter, parses its JSON content as a flat key-value mapping, and sets the resulting pairs as environment variables. This happens before `django.setup()` and only on cold start, following the same pattern as the existing `resolve_secrets_into_env()` in `secret_loader.py`.

## Glossary

- **SSM_Environment_Loader**: The module responsible for reading an SSM Parameter Store parameter and setting its JSON content as environment variables
- **SSM_Parameter**: An AWS Systems Manager Parameter Store parameter containing a JSON object whose keys and values are strings
- **Cold_Start**: The first invocation of a Lambda container, before `django.setup()` has been called
- **Handler**: The Lambda entry point in `handler.py` that orchestrates cold-start setup and SQS record processing

## Requirements

### Requirement 1: Load SSM parameter on cold start

**User Story:** As a developer deploying Lambda tasks, I want environment variables loaded from an SSM parameter at cold start, so that I can manage environment configuration centrally in Parameter Store without baking values into the Lambda deployment package.

#### Acceptance Criteria

1. WHEN the environment variable `LAMBDA_TASKS_SSM_ENVIRONMENT` is set and the Lambda container is performing cold start, THE SSM_Environment_Loader SHALL retrieve the SSM parameter whose name matches the value of `LAMBDA_TASKS_SSM_ENVIRONMENT`
2. WHEN the environment variable `LAMBDA_TASKS_SSM_ENVIRONMENT` is not set, THE SSM_Environment_Loader SHALL take no action and make no AWS API calls
3. WHEN the SSM parameter is retrieved successfully, THE SSM_Environment_Loader SHALL parse the parameter value as a JSON object and set each key-value pair as an environment variable in `os.environ`
4. THE SSM_Environment_Loader SHALL execute before the `os.environ.get("DJANGO_SETTINGS_MODULE")` check in the handler cold-start block
5. THE SSM_Environment_Loader SHALL execute before `resolve_secrets_into_env()` is called during cold start
6. THE SSM_Environment_Loader SHALL execute before `django.setup()` is called during cold start

### Requirement 2: Validate SSM parameter content

**User Story:** As a developer, I want the loader to fail fast on invalid parameter content, so that misconfiguration is caught immediately at cold start rather than causing subtle runtime errors.

#### Acceptance Criteria

1. IF the SSM parameter value is not valid JSON, THEN THE SSM_Environment_Loader SHALL raise a `ValueError` with a descriptive message including the parameter name
2. IF the SSM parameter JSON is not a flat object (i.e. contains non-string values), THEN THE SSM_Environment_Loader SHALL raise a `ValueError` with a descriptive message identifying the offending keys
3. IF the SSM parameter JSON contains an empty string as a key, THEN THE SSM_Environment_Loader SHALL raise a `ValueError` with a descriptive message

### Requirement 3: Idempotent execution

**User Story:** As a developer, I want the loader to be safe to call multiple times, so that warm invocations pay no extra cost and the function can be called defensively.

#### Acceptance Criteria

1. WHEN the SSM_Environment_Loader has already successfully loaded the parameter, THE SSM_Environment_Loader SHALL skip the AWS API call on subsequent invocations and return immediately
2. THE SSM_Environment_Loader SHALL use a module-level cache to store the fetched parameter value for the lifetime of the Lambda container

### Requirement 4: Function signature conventions

**User Story:** As a maintainer, I want the loader to follow the project's coding conventions, so that the codebase remains consistent.

#### Acceptance Criteria

1. THE SSM_Environment_Loader SHALL expose a single public function `resolve_ssm_environment()` that takes no arguments
2. THE SSM_Environment_Loader SHALL reside in a module named `ssm_environment_loader.py` within the `lambda_tasks` package
3. THE SSM_Environment_Loader SHALL use keyword-only arguments for all internal helper functions with full type annotations
