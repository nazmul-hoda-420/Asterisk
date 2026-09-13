# Generated for Lead / LeadList restructuring on 2026-08-19

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def move_existing_leads_into_default_list(apps, schema_editor):
    LeadList = apps.get_model('core', 'LeadList')
    Lead = apps.get_model('core', 'Lead')
    if Lead.objects.exists():
        default_list = LeadList.objects.create(name='Imported')
        Lead.objects.update(lead_list=default_list)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0047_lead'),
    ]

    operations = [
        migrations.CreateModel(
            name='LeadList',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='lead_lists_created', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name_plural': 'Lead Lists',
                'db_table': 'lead_lists',
                'ordering': ['-created_at'],
            },
        ),
        migrations.RemoveField(
            model_name='lead',
            name='added_by',
        ),
        migrations.AddField(
            model_name='lead',
            name='lead_list',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='leads', to='core.leadlist'),
        ),
        migrations.AlterField(
            model_name='lead',
            name='number',
            field=models.CharField(db_index=True, max_length=20),
        ),
        migrations.RunPython(move_existing_leads_into_default_list, noop_reverse),
        migrations.AlterField(
            model_name='lead',
            name='lead_list',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='leads', to='core.leadlist'),
        ),
        migrations.AlterUniqueTogether(
            name='lead',
            unique_together={('lead_list', 'number')},
        ),
    ]
