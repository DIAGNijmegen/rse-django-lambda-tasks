"""Rename SUCCESS → SUCCEEDED and RETRYING → RETRIED status values."""

from django.db import migrations, models


def rename_statuses_forward(apps, schema_editor):
    TaskRecord = apps.get_model("lambda_tasks", "TaskRecord")
    TaskRecord.objects.filter(status="SUCCESS").update(status="SUCCEEDED")
    TaskRecord.objects.filter(status="RETRYING").update(status="RETRIED")


def rename_statuses_backward(apps, schema_editor):
    TaskRecord = apps.get_model("lambda_tasks", "TaskRecord")
    TaskRecord.objects.filter(status="SUCCEEDED").update(status="SUCCESS")
    TaskRecord.objects.filter(status="RETRIED").update(status="RETRYING")


class Migration(migrations.Migration):

    dependencies = [
        ("lambda_tasks", "0001_initial"),
    ]

    operations = [
        # First, drop the existing check constraint so we can update data
        migrations.RemoveConstraint(
            model_name="taskrecord",
            name="taskrecord_status_valid",
        ),
        # Run data migration to rename existing rows
        migrations.RunPython(
            rename_statuses_forward,
            rename_statuses_backward,
        ),
        # Alter the field with new choices
        migrations.AlterField(
            model_name="taskrecord",
            name="status",
            field=models.CharField(
                choices=[
                    ("RUNNING", "Running"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                    ("RETRIED", "Retried"),
                ],
                editable=False,
                max_length=10,
            ),
        ),
        # Re-add the check constraint with updated values
        migrations.AddConstraint(
            model_name="taskrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["RUNNING", "SUCCEEDED", "FAILED", "RETRIED"])
                ),
                name="taskrecord_status_valid",
            ),
        ),
    ]
