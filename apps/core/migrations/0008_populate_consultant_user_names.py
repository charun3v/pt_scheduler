from django.db import migrations


def populate_user_names(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    name_map = {
        'bindhuj': ('Bindhu', 'Joseph'),
        'binduv': ('Bindu', 'V'),
        'vlokesh': ('Lokesh', 'V'),
        'thejaswini': ('Thejaswini', ''),
        'naveent': ('Naveen', 'T'),
        'siddannap': ('Siddanna', 'P'),
        'gov1': ('Govardhan', ''),
        'khaleeli': ('Ibrahim', 'Khaleel'),
        'nandan': ('Nanda', ''),
        'aradhanak': ('Aradhana', 'K'),
        'vinayk': ('Vinay', 'K'),
        'shams': ('Shamsunder', ''),
        'tanvirp': ('Tanvir', 'Pasha'),
        'sridhar': ('Sridhar', ''),
        'udayk': ('Uday', 'Kumar'),
        'nikhilak': ('Nikhila', 'K R'),
        'sunilr': ('Sunil', 'R'),
        'sanjeetm': ('Sanjeeth', 'Mandal'),
        'ashal': ('Ashalatha', ''),
        'pawar': ('Pawar', ''),
        'manjula': ('Manjula', ''),
        'n3v': ('Nithin', 'Bhaskar V'),
        'rashmi': ('Rashmi', ''),
        'ajay': ('Ajay', ''),
    }
    for username, (first, last) in name_map.items():
        u = User.objects.filter(username=username).first()
        if u and not u.first_name and not u.last_name:
            u.first_name = first
            u.last_name = last
            u.save()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_printtemplateconfig_emergency_header_line_3'),
    ]

    operations = [
        migrations.RunPython(populate_user_names, migrations.RunPython.noop),
    ]
