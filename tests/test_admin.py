from unittest.mock import patch

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from django.utils.timezone import now

import lambda_tasks.admin  # noqa: F401 — triggers @admin.register side-effect
from lambda_tasks.models import SQSLambdaTask, TaskRecord, TaskStatus


def test_task_record_registered_in_admin():
    assert TaskRecord in admin.site._registry


def test_task_record_admin_list_display():
    assert admin.site._registry[TaskRecord].list_display == (
        "pk",
        "task_name",
        "queue",
        "status",
        "start_time",
        "end_time",
        "n_retries",
        "duration",
        "result",
    )


@pytest.fixture()
def admin_user(db: None) -> User:
    return User.objects.create_superuser(
        username="admin", password="password", email="admin@example.com"
    )


@pytest.fixture()
def task_record(db: None) -> TaskRecord:
    return TaskRecord.objects.create(
        id="00000000-0000-0000-0000-000000000001",
        task_name="myapp.tasks.my_task",
        kwargs={"user_id": 42},
        n_retries=3,
        status=TaskStatus.FAILED,
        start_time=now(),
        end_time=now(),
    )


@pytest.fixture()
def _mock_import_string():
    class _FakeWrapper:
        queue = "default"

    with patch("lambda_tasks.admin.import_string", return_value=_FakeWrapper()):
        yield


@pytest.mark.django_db()
class TestReplayAction:
    def test_replay_action_is_registered(self) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = User(is_staff=True, is_superuser=True)
        action_names = list(model_admin.get_actions(request).keys())
        assert "replay_tasks" in action_names

    def test_replay_enqueues_task_with_original_kwargs(
        self, admin_user: User, task_record: TaskRecord, _mock_import_string: None
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        with patch(
            "lambda_tasks.admin.SQSLambdaTask.execute_on_commit"
        ) as mock_execute:
            model_admin.replay_tasks(request, queryset)

        mock_execute.assert_called_once()

    def test_replay_resets_n_retries_to_zero(
        self, admin_user: User, task_record: TaskRecord, _mock_import_string: None
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        built_tasks: list[SQSLambdaTask] = []

        def capture_execute(self: SQSLambdaTask) -> None:
            built_tasks.append(self)

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_execute):
            model_admin.replay_tasks(request, queryset)

        assert len(built_tasks) == 1
        assert built_tasks[0].message.n_retries == 0

    def test_replay_preserves_task_name(
        self, admin_user: User, task_record: TaskRecord, _mock_import_string: None
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        built_tasks: list[SQSLambdaTask] = []

        def capture_execute(self: SQSLambdaTask) -> None:
            built_tasks.append(self)

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_execute):
            model_admin.replay_tasks(request, queryset)

        assert built_tasks[0].message.task_name == "myapp.tasks.my_task"
        assert built_tasks[0].message.kwargs == {"user_id": 42}

    def test_replay_uses_delay_zero(
        self, admin_user: User, task_record: TaskRecord, _mock_import_string: None
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        built_tasks: list[SQSLambdaTask] = []

        def capture_execute(self: SQSLambdaTask) -> None:
            built_tasks.append(self)

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_execute):
            model_admin.replay_tasks(request, queryset)

        assert built_tasks[0].delay == 0

    def test_replay_resolves_queue_from_wrapper(
        self, admin_user: User, db: None
    ) -> None:
        record = TaskRecord.objects.create(
            id="00000000-0000-0000-0000-000000000002",
            task_name="myapp.tasks.custom_queue_task",
            kwargs={"x": 1},
            n_retries=0,
            status=TaskStatus.FAILED,
            start_time=now(),
            end_time=now(),
        )

        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=record.pk)

        built_tasks: list[SQSLambdaTask] = []

        def capture_execute(self: SQSLambdaTask) -> None:
            built_tasks.append(self)

        class _FakeWrapper:
            queue = "high-priority"

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_execute):
            with patch(
                "lambda_tasks.admin.import_string",
                return_value=_FakeWrapper(),
            ):
                model_admin.replay_tasks(request, queryset)

        assert built_tasks[0].queue == "high-priority"

    def test_replay_raises_on_import_error(
        self, admin_user: User, task_record: TaskRecord
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        with patch(
            "lambda_tasks.admin.import_string",
            side_effect=ImportError("no such module"),
        ):
            with pytest.raises(ImportError, match="no such module"):
                model_admin.replay_tasks(request, queryset)

    def test_replay_raises_when_not_wrapper(
        self, admin_user: User, task_record: TaskRecord
    ) -> None:
        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk=task_record.pk)

        with patch(
            "lambda_tasks.admin.import_string",
            return_value="not a wrapper",
        ):
            with pytest.raises(AttributeError):
                model_admin.replay_tasks(request, queryset)

    def test_replay_multiple_records(
        self, admin_user: User, db: None, _mock_import_string: None
    ) -> None:
        records = [
            TaskRecord.objects.create(
                id=f"00000000-0000-0000-0000-00000000000{i}",
                task_name=f"myapp.tasks.task_{i}",
                kwargs={"key": i},
                n_retries=0,
                status=TaskStatus.FAILED,
                start_time=now(),
                end_time=now(),
            )
            for i in range(3)
        ]

        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().post("/")
        request.user = admin_user

        queryset = TaskRecord.objects.filter(pk__in=[r.pk for r in records])

        built_tasks: list[SQSLambdaTask] = []

        def capture_execute(self: SQSLambdaTask) -> None:
            built_tasks.append(self)

        with patch.object(SQSLambdaTask, "execute_on_commit", capture_execute):
            model_admin.replay_tasks(request, queryset)

        assert len(built_tasks) == 3
        task_names = {t.message.task_name for t in built_tasks}
        assert task_names == {
            "myapp.tasks.task_0",
            "myapp.tasks.task_1",
            "myapp.tasks.task_2",
        }

    def test_replay_action_hidden_without_change_permission(self, db: None) -> None:
        user = User.objects.create_user(
            username="viewer", password="password", is_staff=True
        )
        content_type = ContentType.objects.get_for_model(TaskRecord)
        view_perm = Permission.objects.get(
            codename="view_taskrecord", content_type=content_type
        )
        user.user_permissions.add(view_perm)

        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().get("/")
        request.user = user

        action_names = list(model_admin.get_actions(request).keys())
        assert "replay_tasks" not in action_names

    def test_replay_action_visible_with_change_permission(self, db: None) -> None:
        user = User.objects.create_user(
            username="editor", password="password", is_staff=True
        )
        content_type = ContentType.objects.get_for_model(TaskRecord)
        change_perm = Permission.objects.get(
            codename="change_taskrecord", content_type=content_type
        )
        user.user_permissions.add(change_perm)

        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().get("/")
        request.user = user

        action_names = list(model_admin.get_actions(request).keys())
        assert "replay_tasks" in action_names


@pytest.mark.django_db()
class TestKwargsSearchUsesIndex:
    def test_admin_search_uses_gin_trigram_indexes(self) -> None:
        """Verify the GIN trigram indexes are used for admin search queries."""
        from django.db import connection
        from django.test import RequestFactory

        model_admin = admin.site._registry[TaskRecord]
        request = RequestFactory().get("/", data={"q": "29992d75"})
        request.user = User(is_staff=True, is_superuser=True)

        changelist = model_admin.get_changelist_instance(request)
        queryset = changelist.get_queryset(request)

        sql, params = queryset.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute("SET enable_seqscan = off")
            cursor.execute("SET enable_indexscan = off")
            cursor.execute(f"EXPLAIN {sql}", params)
            plan = "\n".join(row[0] for row in cursor.fetchall())
            cursor.execute("SET enable_indexscan = on")
            cursor.execute("SET enable_seqscan = on")

        assert "taskrecord_id_trgm" in plan
        assert "taskrecord_kwargs_trgm" in plan
