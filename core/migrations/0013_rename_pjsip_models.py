# Renames the PJSIP-prefixed model classes to drop the "PJSIP" prefix (e.g. PJSIPContact ->
# Contact). db_table stays the same for every model, so this is a Django-state-only change —
# no ALTER TABLE runs against the (already Asterisk-managed) tables.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_pjsipregistration'),
    ]

    operations = [
        migrations.RenameModel(old_name='PJSIPAuth', new_name='Auth'),
        migrations.RenameModel(old_name='PJSIPAor', new_name='Aor'),
        migrations.RenameModel(old_name='PJSIPEndpointIdIp', new_name='EndpointIdIp'),
        migrations.RenameModel(old_name='PJSIPEndpoint', new_name='Endpoint'),
        migrations.RenameModel(old_name='PJSIPRegistration', new_name='Registration'),
        migrations.RenameModel(old_name='PJSIPContact', new_name='Contact'),
    ]
