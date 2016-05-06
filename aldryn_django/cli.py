#-*- coding: utf-8 -*-
from __future__ import absolute_import
import click
import os
import sys
from pipes import quote
from django.template import loader, Context
from django.conf import settings as django_settings
from aldryn_addons.utils import openfile, boolean_ish, senv


# add the current directory to pythonpath. So the project files can be read.
BASE_DIR = os.getcwd()
sys.path.insert(0, BASE_DIR)


@click.command()
@click.pass_obj
def web(ctx_obj):
    """
    launch the webserver of choice (uwsgi)
    """
    if any(boolean_ish(ctx_obj['settings'][key]) for key in ['ENABLE_NGINX', 'ENABLE_PAGESPEED', 'ENABLE_BROWSERCACHE']):
        # uwsgi behind nginx. possibly with pagespeed/browsercache
        start_with_nginx(ctx_obj['settings'])
    else:
        # pure uwsgi
        execute(start_uwsgi_command(settings=ctx_obj['settings'], port=80))


@click.command()
@click.pass_obj
def worker(ctx_obj):
    """
    coming soon: launch the background worker
    """
    # TODO: celery worker startup, once available
    pass


@click.command()
@click.pass_obj
def migrate(ctx_obj):
    """
    run any migrations needed at deploy time. most notably database migrations.
    """
    cmds = ctx_obj['settings']['MIGRATION_COMMANDS']
    click.echo('aldryn-django: running migration commands')
    for cmd in cmds:
        click.echo('    ----> {}'.format(cmd))
        exitcode = os.system(cmd)
        if exitcode != 0:
            sys.exit(exitcode)


@click.group()
@click.option('--verbose', is_flag=True)
@click.pass_context
def main(ctx, verbose):
    if not os.path.exists(os.path.join(BASE_DIR, 'manage.py')):
        raise click.UsageError('make sure you are in the same directory as manage.py')

    if verbose:
        os.environ['ALDRYN_ADDONS_DEBUG'] = 'True'

    from . import startup
    startup._setup(BASE_DIR)

    ctx.obj = {
        'settings': {key: getattr(django_settings, key) for key in dir(django_settings)}
    }


main.add_command(web)
main.add_command(worker)
main.add_command(migrate)


def get_env():
    # setup default uwsgi environment variables
    env = {
        'UWSGI_ENABLE_THREADS': '1',
    }
    if boolean_ish(os.environ.get('ENABLE_UWSGI_CHEAPER', 'on')):
        env.update({
            'UWSGI_CHEAPER': '1',
            'UWSGI_CHEAPER_ALGO': 'busyness',
            'UWSGI_CHEAPER_INITIAL': '1',
            'UWSGI_CHEAPER_BUSINESS_VERBOSE': '1',
            'UWSGI_CHEAPER_BUSINESS_BACKLOG_ALERT': '10',
            'UWSGI_CHEAPER_OVERLOAD': '30',
        })
    env.update(os.environ)
    return env


def execute(args, script=None):
    # TODO: is cleanup needed before calling exec? (open files, ...)
    command = script or args[0]
    os.execvpe(command, args, get_env())


def start_uwsgi_command(settings, port=None):
    cmd = [
        'uwsgi',
        '--module=wsgi',
        '--http=0.0.0.0:{}'.format(port or settings.get('PORT')),
        '--master',
        '--workers={}'.format(settings['DJANGO_WEB_WORKERS']),
        '--max-requests={}'.format(settings['DJANGO_WEB_MAX_REQUESTS']),
        '--harakiri={}'.format(settings['DJANGO_WEB_TIMEOUT']),
        '--lazy-apps',
    ]

    serve_static = False

    if not settings['ENABLE_SYNCING']:
        if not settings['STATIC_URL_IS_ON_OTHER_DOMAIN']:
            serve_static = True
            cmd.extend([
                '--static-map={}={}'.format(
                    settings['STATIC_URL'],
                    settings['STATIC_ROOT'],
                ),
                # Set far-future expiration headers for django-compressor
                # generated files
                '--static-expires={} 31536000'.format(
                    os.path.join(
                        settings['STATIC_ROOT'],
                        settings.get('COMPRESS_OUTPUT_DIR', 'CACHE'),
                        '.*',
                    )
                ),
                # Set far-future expiration headers for static files with
                # hashed filenames
                '--static-expires={} 31536000'.format(
                    os.path.join(
                        settings['STATIC_ROOT'],
                        r'.*\.[0-9a-f]{10,16}\.[a-z]+',
                    )
                ),
            ])

        if not settings['MEDIA_URL_IS_ON_OTHER_DOMAIN']:
            serve_static = True
            cmd.append('--static-map={}={}'.format(
                settings['MEDIA_URL'],
                settings['MEDIA_ROOT'],
            ))

        if serve_static:
            cmd.extend([
                # Start 2 offloading threads for each worker
                '--offload-threads=2',
                # Cache resolved paths for up to 1 day (limited to 5k entries
                # of max 1kB size each)
                '--static-cache-paths=86400',
                '--static-cache-paths-name=staticpaths',
                '--cache2=name=staticpaths,items=5000,blocksize=1k,purge_lru,ignore_full',
                # Serve .gz files if that version is available
                '--static-gzip-all',
            ])
    print(cmd)
    return cmd


def start_procfile_command(procfile_path):
    return [
        'forego',
        'start',
        '-f',
        procfile_path
    ]


def start_with_nginx(settings):
    # TODO: test with pagespeed and static or media on other domain
    if not all([settings['NGINX_CONF_PATH'], settings['NGINX_PROCFILE_PATH']]):
        raise click.UsageError('NGINX_CONF_PATH and NGINX_PROCFILE_PATH must be configured')

    commands = {
        'nginx': 'nginx',
        'django': ' '.join(quote(c) for c in start_uwsgi_command(
            settings, port=settings['BACKEND_PORT']))
    }

    procfile = '\n'.join(
        '{}: {}'.format(name, command)
        for name, command in commands.items()
    )
    nginx_template = loader.get_template('aldryn_django/configuration/nginx.conf')
    context = Context(dict(settings))
    nginx_conf = nginx_template.render(context)
    with openfile(settings['NGINX_CONF_PATH']) as f:
        f.write(nginx_conf)
    with openfile(settings['NGINX_PROCFILE_PATH']) as f:
        f.write(procfile)
    execute(start_procfile_command(settings['NGINX_PROCFILE_PATH']))
