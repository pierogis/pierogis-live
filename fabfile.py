import os

import boto3
import dotenv
from fabric import Connection, task, SerialGroup
# the servers where the commands are executed
from setuptools import sandbox

from pierogis_live import __version__

dotenv.load_dotenv()


@task
def build(context):
    # build the package
    sandbox.run_setup('setup.py', ['clean', 'sdist'])


@task(optional=['aws_region', 'aws_subnet_id', 'count'])
def launch(context, stage, aws_region=None, aws_subnet_id=None, count=1):
    aws_region = aws_region or os.getenv('AWS_REGION')
    aws_subnet_id = aws_subnet_id or os.getenv('AWS_SUBNET_ID')

    res = boto3.resource('ec2', aws_region)
    client = boto3.client('ec2', aws_region)

    allocated_addresses = get_stage_addresses(client, stage, allocated=True)
    if len(allocated_addresses) != 0:
        client.terminate_instances(InstanceIds=[address.get('InstanceId') for address in allocated_addresses])

    instances = res.create_instances(
        MaxCount=count,
        LaunchTemplate={
            'LaunchTemplateName': 'pierogis-live',
            'Version': '8',
        },
        MinCount=count,
        SubnetId=aws_subnet_id
    )

    addresses = get_stage_addresses(client, stage, allocated=False)

    for i in range(count):
        instance = instances[i]
        address = addresses[i]

        instance.wait_until_running()

        client.associate_address(AllocationId=address['AllocationId'],
                                 InstanceId=instance.instance_id)


def get_connect_kwargs():
    aws_key_file = os.getenv('AWS_KEY_FILE')
    connect_kwargs = {}
    if aws_key_file is not None:
        connect_kwargs['key_filename'] = os.environ['AWS_KEY_FILE']

    return connect_kwargs


@task(optional=['aws_region'])
def bootstrap(context, stage, aws_region=None):
    aws_region = aws_region or os.getenv('AWS_REGION')
    client = boto3.client('ec2', aws_region)

    allocated_addresses = get_stage_addresses(client, stage, allocated=True)
    ips = [address.get('PublicIp') for address in allocated_addresses]

    connect_kwargs = get_connect_kwargs()

    group = SerialGroup(*ips, user='ec2-user', connect_kwargs=connect_kwargs)

    # install nginx
    group.run("sudo amazon-linux-extras install -y nginx1")

    # install python3
    group.run("sudo yum install -y python3 python3-devel gcc postgresql-devel")

    # add service executing user
    group.run("sudo useradd pierogis-live")
    group.run("sudo passwd -f -u pierogis-live")

    print("installing python dependencies")
    # create the python environment for the deployment
    group.run("sudo python3 -m venv /home/pierogis-live/venv")
    group.run("sudo /home/pierogis-live/venv/bin/pip install gunicorn")
    group.run("sudo /home/pierogis-live/venv/bin/pip install psycopg2")

    print("making gunicorn log fiiles")
    # create gunicorn log files
    group.run("sudo mkdir /home/pierogis-live/log")
    group.run("sudo mkdir /home/pierogis-live/conf")
    group.run("sudo touch /home/pierogis-live/log/gunicorn.access.log")
    group.run("sudo touch /home/pierogis-live/log/gunicorn.error.log")

    print("giving nginx access to pierogis-live home")
    # make pierogis-live owner of their home folder
    group.run("sudo chown -R pierogis-live:pierogis-live /home/pierogis-live")

    # and give nginx service access to pierogis-live home folder
    group.run("sudo chmod 733 /home/pierogis-live/")
    group.run("sudo usermod -a -G pierogis-live nginx")

    print("creating dirs for nginx and gunicorn service configs")
    # create dir for nginx and system d config
    group.run('sudo mkdir /usr/local/lib/systemd')
    group.run('sudo mkdir /usr/local/lib/systemd/system')


@task(optional=['content_home', 'cdn_url', 'database_server_url', 'aws_region'])
def deploy(context, stage, content_home=None, cdn_url=None,
           database_server_url=None,
           aws_region=None):
    aws_region = aws_region or os.getenv('AWS_REGION')

    client = boto3.client('ec2', aws_region)
    allocated_addresses = get_stage_addresses(client, stage, allocated=True)
    ips = [address.get('PublicIp') for address in allocated_addresses]

    group = SerialGroup(*ips, user='ec2-user', connect_kwargs=get_connect_kwargs())

    # figure out the package name and version
    # dist = subprocess.run('python setup.py --fullname', capture_output=True).strip()

    filename = '{}-{}.tar.gz'.format('pierogis-live', __version__)

    print("Copying gunicorn and nginx conf")

    for connection in group:
        # copy service configs
        connection.put('bootstrap/pierogis-live-nginx.conf', '/tmp/pierogis-live-nginx.conf')
        connection.put('bootstrap/pierogis-live.service', '/tmp/pierogis-live.service')

        # copy gunicorn config
        connection.put('bootstrap/gunicorn.conf.py', '/tmp/gunicorn.conf.py')

        # upload the package to the temporary folder on the server
        connection.put('dist/%s' % filename, '/tmp/%s' % filename)

    group.run("sudo mv /tmp/pierogis-live-nginx.conf /etc/nginx/conf.d/pierogis-live-nginx.conf")
    group.run("sudo mv /tmp/pierogis-live.service /usr/local/lib/systemd/system/pierogis-live.service")
    group.run("sudo mv /tmp/gunicorn.conf.py /home/pierogis-live/conf/gunicorn.conf.py")

    # add env var to .env
    server_name = os.environ['SERVER_NAME']
    content_home = content_home or os.environ['CONTENT_HOME']

    if database_server_url is None:
        database_server_url = os.environ['DATABASE_SERVER_URL']

    if cdn_url is None:
        cdn_url = os.environ['CDN_URL']

    jwt_token_location = os.environ['JWT_TOKEN_LOCATION']
    secret_key = os.environ['SECRET_KEY']

    print("Adding env var to .env:")

    group.run('echo SERVER_NAME={} | sudo tee -a /home/pierogis-live/.env'.format(server_name))
    group.run('echo SECRET_KEY={} | sudo tee -a /home/pierogis-live/.env'.format(secret_key))
    group.run('echo CONTENT_HOME={} | sudo tee -a /home/pierogis-live/.env'.format(content_home))
    group.run('echo DATABASE_SERVER_URL={} | sudo tee -a /home/pierogis-live/.env'.format(database_server_url))
    group.run('echo CDN_URL={} | sudo tee -a /home/pierogis-live/.env'.format(cdn_url))
    group.run('echo JWT_TOKEN_LOCATION={} | sudo tee -a /home/pierogis-live/.env'.format(jwt_token_location))
    group.run('echo STAGE={} | sudo tee -a /home/pierogis-live/.env'.format(stage))

    group.run('echo FLASK_APP={} | sudo tee -a /home/pierogis-live/.flaskenv'.format('pierogis_live'))

    # install the package in the application's virtualenv with pip
    group.run('sudo /home/pierogis-live/venv/bin/pip install /tmp/%s' % filename)

    # remove the uploaded package
    group.run('rm -r /tmp/%s' % filename)
    group.run("sudo chmod 733 /home/pierogis-live/")

    connection = group[0]

    print("Checking for migrations")

    # make directories for migrations files
    connection.run("sudo rm -rf /tmp/migrations")
    connection.run('sudo mkdir -m 777 /tmp/migrations')
    connection.run('sudo mkdir -m 777 /tmp/migrations/versions')

    # get migration files and put them all in the ec2 instance
    version_files = os.listdir('migrations/versions')

    for filename in version_files:
        if filename == '__pycache__':
            continue
        connection.put('migrations/versions/' + filename, '/tmp/migrations/versions/' + filename)

    connection.put('migrations/alembic.ini', '/tmp/migrations/alembic.ini')
    connection.put('migrations/env.py', '/tmp/migrations/env.py')

    connection.run("sudo rm -rf /home/pierogis-live/migrations")
    connection.run("sudo mv /tmp/migrations /home/pierogis-live")
    connection.run("cd /home/pierogis-live && venv/bin/flask db upgrade")

    print("Starting server and nginx")

    # restart services
    group.run("sudo systemctl enable pierogis-live")
    group.run("sudo systemctl restart pierogis-live")
    group.run("sudo systemctl enable nginx")
    group.run("sudo systemctl restart nginx")


def get_stage_addresses(client, stage: str, allocated=False):
    # find the elastic ip for the desired stage
    elastic_ip_filters = [
        {'Name': 'domain', 'Values': ['vpc']},
        {'Name': 'tag-key', 'Values': ['pierogis-' + stage]},
    ]
    if allocated:
        elastic_ip_filters.append({'Name': 'instance-id', 'Values': ['*']})

    return client.describe_addresses(Filters=elastic_ip_filters)['Addresses']