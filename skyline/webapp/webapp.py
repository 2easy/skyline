from __future__ import division
import redis
import logging
import simplejson as json
import sys
import re
import csv
import traceback
from msgpack import Unpacker
from functools import wraps
from flask import (
    Flask, request, render_template, redirect, Response, abort, flash,
    send_file)
from daemon import runner
from os.path import isdir
from os import path
import string
from os import remove as os_remove
from time import time, sleep

# @added 20160703 - Feature #1464: Webapp Redis browser
import time
from datetime import datetime, timedelta
import os
import base64
# flask things for rebrow
from flask import session, g, url_for, flash, Markup, json
# For secret_key
import uuid

from logging.handlers import TimedRotatingFileHandler, MemoryHandler

import os.path
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir))
sys.path.insert(0, os.path.dirname(__file__))
import settings
from validate_settings import validate_settings_variables
import skyline_version
from skyline_functions import get_graphite_metric

from backend import panorama_request, get_list
from ionosphere_backend import (
    ionosphere_data, ionosphere_metric_data,
    ionosphere_get_metrics_dir, create_features_profile,
    features_profile_details)
from features_profile import feature_name_id, calculate_features_profile
from tsfresh_feature_names import TSFRESH_VERSION

skyline_version = skyline_version.__absolute_version__

skyline_app = 'webapp'
skyline_app_logger = '%sLog' % skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
skyline_app_loglock = '%s.lock' % skyline_app_logfile
skyline_app_logwait = '%s.wait' % skyline_app_logfile
logfile = '%s/%s.log' % (settings.LOG_PATH, skyline_app)

# werkzeug access log
access_logger = logging.getLogger('werkzeug')

REDIS_CONN = redis.StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH)

# ENABLE_WEBAPP_DEBUG = True

app = Flask(__name__)

gunicorn_error_logger = logging.getLogger('gunicorn.error')
logger.handlers.extend(gunicorn_error_logger.handlers)
logger.setLevel(logging.DEBUG)

# app.secret_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, settings.GRAPHITE_HOST))
secret_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, settings.GRAPHITE_HOST))
app.secret_key = secret_key

app.config['PROPAGATE_EXCEPTIONS'] = True

app.config.update(
    SESSION_COOKIE_NAME='skyline',
    SESSION_COOKIE_SECURE=True,
    SECRET_KEY=secret_key
)

graph_url_string = str(settings.GRAPH_URL)
PANORAMA_GRAPH_URL = re.sub('\/render\/.*', '', graph_url_string)

# @added 20160727 - Bug #1524: Panorama dygraph not aligning correctly
# Defaults for momentjs to work if the setttings.py was not updated
try:
    WEBAPP_USER_TIMEZONE = settings.WEBAPP_USER_TIMEZONE
except:
    WEBAPP_USER_TIMEZONE = True
try:
    WEBAPP_FIXED_TIMEZONE = settings.WEBAPP_FIXED_TIMEZONE
except:
    WEBAPP_FIXED_TIMEZONE = 'Etc/GMT+0'
try:
    WEBAPP_JAVASCRIPT_DEBUG = settings.WEBAPP_JAVASCRIPT_DEBUG
except:
    WEBAPP_JAVASCRIPT_DEBUG = False


@app.before_request
#def setup_logging():
#    if not app.debug:
#        stream_handler = logging.StreamHandler()
#        stream_handler.setLevel(logging.DEBUG)
#        app.logger.addHandler(stream_handler)
#        import logging
#        from logging.handlers import TimedRotatingFileHandler, MemoryHandler
#        file_handler = MemoryHandler(app_logfile, mode='a')
#        file_handler.setLevel(logging.DEBUG)
#        app.logger.addHandler(file_handler)

#        formatter = logging.Formatter("%(asctime)s :: %(process)s :: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
#        handler = logging.handlers.TimedRotatingFileHandler(
#            app_logfile,
#            when="midnight",
#            interval=1,
#            backupCount=5)
#        handler.setLevel(logging.DEBUG)

#        memory_handler = logging.handlers.MemoryHandler(100,
#                                                        flushLevel=logging.DEBUG,
#                                                        target=handler)
#        handler.setFormatter(formatter)
#        app.logger.addHandler(memory_handler)
#        app.logger.addHandler(handler)
def limit_remote_addr():
    """
    This function is called to check if the requesting IP address is in the
    settings.WEBAPP_ALLOWED_IPS array, if not 403.
    """
    ip_allowed = False
    for web_allowed_ip in settings.WEBAPP_ALLOWED_IPS:
        if request.remote_addr == web_allowed_ip:
            ip_allowed = True

    if not settings.WEBAPP_IP_RESTRICTED:
        ip_allowed = True

    if not ip_allowed:
        abort(403)  # Forbidden


def check_auth(username, password):
    """This function is called to check if a username /
    password combination is valid.
    """
    if settings.WEBAPP_AUTH_ENABLED:
        return username == settings.WEBAPP_AUTH_USER and password == settings.WEBAPP_AUTH_USER_PASSWORD
    else:
        return True


def authenticate():
    """Sends a 401 response that enables basic auth"""
    return Response(
        'Forbidden', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if settings.WEBAPP_AUTH_ENABLED:
            auth = request.authorization
            if not auth or not check_auth(auth.username, auth.password):
                return authenticate()
            return f(*args, **kwargs)
        else:
            return True
    return decorated


@app.errorhandler(500)
def internal_error(message, traceback_format_exc):
    """
    Show traceback in the browser when running a flask app on a production
    server.
    By default, flask does not show any useful information when running on a
    production server.
    By adding this view, we output the Python traceback to the error 500 page
    and log.

    As per:
    - Show flask traceback when running on production server -
      https://gist.github.com/probonopd/8616a8ff05c8a75e4601 - Python traceback
      rendered nicely by Jinja2

    This can be tested by hitting SKYLINE_URL/a_500

    """
    fail_msg = str(message)
    trace = str(traceback_format_exc)
    logger.debug('debug :: returning 500 as there was an error, why else would 500 be returned...')
    logger.debug('debug :: sending the user that caused the error to happen, this useful information')
    logger.debug('debug :: but they or I may have already emailed it to you, you should check your inbox')
    logger.debug('%s' % str(traceback_format_exc))
    logger.debug('debug :: which is accompanied with the message')
    logger.debug('debug :: %s' % str(message))
    logger.debug('debug :: request url :: %s' % str(request.url))
    logger.debug('debug :: request referrer :: %s' % str(request.referrer))

    resp = '<pre>%s</pre><pre>%s</pre>' % (str(message), str(traceback_format_exc))
#    return(resp), 500
    server_name = settings.SERVER_METRICS_NAME
    return render_template(
        'traceback.html', version=skyline_version,
        message=fail_msg, traceback=trace, bad_machine=server_name), 500


@app.route("/")
@requires_auth
def index():

    start = time.time()
    if 'uh_oh' in request.args:
        try:
            return render_template(
                'uh_oh.html', version=skyline_version,
                message="Testing uh_oh"), 200
        except:
            error_string = traceback.format_exc()
            logger.error('error :: failed to render uh_oh.html: %s' % str(error_string))
            return 'Uh oh ... a Skyline 500 :(', 500

    try:
        return render_template(
            'now.html', version=skyline_version,
            duration=(time.time() - start)), 200
    except:
        error_string = traceback.format_exc()
        logger.error('error :: failed to render index.html: %s' % str(error_string))
        return 'Uh oh ... a Skyline 500 :(', 500


@app.route("/a_500")
@requires_auth
def a_500():

    if 'message' in request.args:
        message = request.args.get(str('message'), None)
        logger.debug('debug :: message - %s' % str(message))
        test_500_string = message
    else:
        logger.debug('debug :: testing /a_500 route and app.errorhandler(500) internal_error function')
        message = 'Testing app.errorhandler(500) internal_error function, if you are seeing this it works - OK'
        test_500_string = 'This is a test to generate a ValueError and a HTTP response of 500 and display the traceback'

    try:
        test_errorhandler_500_internal_error = int(test_500_string)
    except:
        trace = traceback.format_exc()
        logger.debug('debug :: test OK')
        return internal_error(message, trace)

    error_msg = 'failed test of /a_500 route and app.errorhandler(500) internal_error function'
    logger.error('error :: %s' % error_msg)
    resp = json.dumps({'results': error_msg})
    return resp, 501


@app.route("/now")
@requires_auth
def now():
    start = time.time()
    try:
        return render_template(
            'now.html', version=skyline_version, duration=(time.time() - start)), 200
    except:
        error_string = traceback.format_exc()
        logger.error('error :: failed to render now.html: %s' % str(error_string))
        return 'Uh oh ... a Skyline 500 :(', 500


@app.route("/then")
@requires_auth
def then():
    start = time.time()
    try:
        return render_template('then.html'), 200
    except:
        error_string = traceback.format_exc()
        logger.error('error :: failed to render then.html: %s' % str(error_string))
        return 'Uh oh ... a Skyline 500 :(', 500


@app.route("/anomalies.json")
def anomalies():
    try:
        anomalies_json = path.abspath(path.join(path.dirname(__file__), '..', settings.ANOMALY_DUMP))
        with open(anomalies_json, 'r') as f:
            json_data = f.read()
    except:
        logger.error('error :: failed to get anomalies.json: ' + traceback.format_exc())
        return 'Uh oh ... a Skyline 500 :(', 500
    return json_data, 200


@app.route("/panorama.json")
def panorama_anomalies():
    try:
        anomalies_json = path.abspath(path.join(path.dirname(__file__), '..', settings.ANOMALY_DUMP))
        panorama_json = string.replace(str(anomalies_json), 'anomalies.json', 'panorama.json')
        logger.info('opening - %s' % panorama_json)
        with open(panorama_json, 'r') as f:
            json_data = f.read()
    except:
        logger.error('error :: failed to get panorama.json: ' + traceback.format_exc())
        return 'Uh oh ... a Skyline 500 :(', 500
    return json_data, 200


@app.route("/app_settings")
def app_settings():

    try:
        app_settings = {'GRAPH_URL': settings.GRAPH_URL,
                        'OCULUS_HOST': settings.OCULUS_HOST,
                        'FULL_NAMESPACE': settings.FULL_NAMESPACE,
                        'SKYLINE_VERSION': skyline_version,
                        'PANORAMA_ENABLED': settings.PANORAMA_ENABLED,
                        'PANORAMA_DATABASE': settings.PANORAMA_DATABASE,
                        'PANORAMA_DBHOST': settings.PANORAMA_DBHOST,
                        'PANORAMA_DBPORT': settings.PANORAMA_DBPORT,
                        'PANORAMA_DBUSER': settings.PANORAMA_DBUSER,
                        'PANORAMA_DBUSERPASS': 'redacted',
                        'PANORAMA_GRAPH_URL': PANORAMA_GRAPH_URL,
                        'WEBAPP_USER_TIMEZONE': settings.WEBAPP_USER_TIMEZONE,
                        'WEBAPP_FIXED_TIMEZONE': settings.WEBAPP_FIXED_TIMEZONE,
                        'WEBAPP_JAVASCRIPT_DEBUG': settings.WEBAPP_JAVASCRIPT_DEBUG
                        }
    except Exception as e:
        error = "error: " + e
        resp = json.dumps({'app_settings': error})
        return resp, 500

    resp = json.dumps(app_settings)
    return resp, 200


@app.route("/version")
def version():

    try:
        version_settings = {'SKYLINE_VERSION': skyline_version}
        resp = json.dumps(version_settings)
        return resp, 200
    except:
        return "Not Found", 404


@app.route("/api", methods=['GET'])
def data():
    if 'metric' in request.args:
        metric = request.args.get(str('metric'), None)
        try:
            raw_series = REDIS_CONN.get(metric)
            if not raw_series:
                resp = json.dumps(
                    {'results': 'Error: No metric by that name - try /api?metric=' + settings.FULL_NAMESPACE + 'metric_namespace'})
                return resp, 404
            else:
                unpacker = Unpacker(use_list=False)
                unpacker.feed(raw_series)
                timeseries = [item[:2] for item in unpacker]
                resp = json.dumps({'results': timeseries})
                return resp, 200
        except Exception as e:
            error = "Error: " + e
            resp = json.dumps({'results': error})
            return resp, 500

    if 'graphite_metric' in request.args:
        logger.info('processing graphite_metric api request')
        for i in request.args:
            key = str(i)
            value = request.args.get(key, None)
            logger.info('request argument - %s=%s' % (key, str(value)))

        valid_request = True
        missing_arguments = []

        metric = request.args.get('graphite_metric', None)
        from_timestamp = request.args.get('from_timestamp', None)
        until_timestamp = request.args.get('until_timestamp', None)

        if not metric:
            valid_request = False
            missing_arguments.append('graphite_metric')
            logger.error('graphite_metric argument not found')
        else:
            logger.info('graphite_metric - %s' % metric)

        if not from_timestamp:
            valid_request = False
            missing_arguments.append('from_timestamp')
            logger.error('from_timestamp argument not found')
        else:
            logger.info('from_timestamp - %s' % str(from_timestamp))

        if not until_timestamp:
            valid_request = False
            missing_arguments.append('until_timestamp')
        else:
            logger.info('until_timestamp - %s' % str(until_timestamp))

        if not valid_request:
            error = 'Error: not all arguments where passed, missing ' + str(missing_arguments)
            resp = json.dumps({'results': error})
            return resp, 404
        else:
            logger.info('requesting data from graphite for %s from %s to %s' % (
                str(metric), str(from_timestamp), str(until_timestamp)))

        try:
            timeseries = get_graphite_metric(
                skyline_app, metric, from_timestamp, until_timestamp, 'json',
                'object')
        except:
            error = "Error: " + traceback.print_exc()
            resp = json.dumps({'results': error})
            return resp, 500

        resp = json.dumps({'results': timeseries})
        cleaned_resp = False
        try:
            format_resp_1 = string.replace(str(resp), '"[[', '[[')
            cleaned_resp = string.replace(str(format_resp_1), ']]"', ']]')
        except:
            logger.error('error :: failed string replace resp: ' + traceback.format_exc())

        if cleaned_resp:
            return cleaned_resp, 200
        else:
            resp = json.dumps(
                {'results': 'Error: failed to generate timeseries'})
            return resp, 404

    resp = json.dumps(
        {'results': 'Error: No argument passed - try /api?metric= or /api?graphite_metric='})
    return resp, 404


@app.route("/docs")
@requires_auth
def docs():
    start = time.time()
    try:
        return render_template(
            'docs.html', version=skyline_version, duration=(time.time() - start)), 200
    except:
        return 'Uh oh ... a Skyline 500 :(', 500


@app.route("/panorama", methods=['GET'])
@requires_auth
def panorama():
    if not settings.PANORAMA_ENABLED:
        try:
            return render_template(
                'uh_oh.html', version=skyline_version,
                message="Panorama is not enabled, please see the Panorama section in the docs and settings.py"), 200
        except:
            return 'Uh oh ... a Skyline 500 :(', 500

    start = time.time()

    try:
        apps = get_list('app')
    except:
        logger.error('error :: %s' % traceback.print_exc())
        apps = ['None']
    try:
        sources = get_list('source')
    except:
        logger.error('error :: %s' % traceback.print_exc())
        sources = ['None']
    try:
        algorithms = get_list('algorithm')
    except:
        logger.error('error :: %s' % traceback.print_exc())
        algorithms = ['None']
    try:
        hosts = get_list('host')
    except:
        logger.error('error :: %s' % traceback.print_exc())
        hosts = ['None']

    request_args_present = False
    try:
        request_args_len = len(request.args)
        request_args_present = True
    except:
        request_args_len = 0

    # @added 20160803 - Sanitize request.args
    REQUEST_ARGS = ['from_date',
                    'from_time',
                    'from_timestamp',
                    'until_date',
                    'until_time',
                    'until_timestamp',
                    'count_by_metric',
                    'metric',
                    'metric_like',
                    'app',
                    'source',
                    'host',
                    'algorithm',
                    'limit',
                    'order',
                    # @added 20161127 - Branch #922: ionosphere
                    'panorama_anomaly_id',
                    ]

    get_anomaly_id = False
    if request_args_present:
        for i in request.args:
            key = str(i)
            if key not in REQUEST_ARGS:
                logger.error('error :: invalid request argument - %s=%s' % (key, str(i)))
                return 'Bad Request', 400
            value = request.args.get(key, None)
            logger.info('request argument - %s=%s' % (key, str(value)))

            # @added 20161127 - Branch #922: ionosphere
            if key == 'panorama_anomaly_id':
                if str(value) == 'true':
                    get_anomaly_id = True

            if key == 'metric' and value != 'all':
                if value != '':
                    try:
                        unique_metrics = list(REDIS_CONN.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))
                    except:
                        logger.error('error :: Webapp could not get the unique_metrics list from Redis')
                        logger.info(traceback.format_exc())
                        return 'Internal Server Error', 500
                    metric_name = settings.FULL_NAMESPACE + value
                    if metric_name not in unique_metrics:
                        error_string = 'error :: no metric - %s - exists in Redis' % metric_name
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'count_by_metric':
                count_by_metric_invalid = True
                if value == 'false':
                    count_by_metric_invalid = False
                if value == 'true':
                    count_by_metric_invalid = False
                if count_by_metric_invalid:
                    error_string = 'error :: invalid %s value passed %s' % (key, value)
                    logger.error('error :: invalid %s value passed %s' % (key, value))
                    return 'Bad Request', 400

            if key == 'metric_like':
                if value == 'all':
                    metric_namespace_pattern = value.replace('all', '')

                metric_namespace_pattern = value.replace('%', '')
                if metric_namespace_pattern != '' and value != 'all':
                    try:
                        unique_metrics = list(REDIS_CONN.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))
                    except:
                        logger.error('error :: Webapp could not get the unique_metrics list from Redis')
                        logger.info(traceback.format_exc())
                        return 'Internal Server Error', 500

                    matching = [s for s in unique_metrics if metric_namespace_pattern in s]
                    if len(matching) == 0:
                        error_string = 'error :: no metric like - %s - exists in Redis' % metric_namespace_pattern
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'from_timestamp' or key == 'until_timestamp':
                timestamp_format_invalid = True
                if value == 'all':
                    timestamp_format_invalid = False
                # unix timestamp
                if value.isdigit():
                    timestamp_format_invalid = False
                # %Y%m%d %H:%M timestamp
                if timestamp_format_invalid:
                    value_strip_colon = value.replace(':', '')
                    new_value = value_strip_colon.replace(' ', '')
                    if new_value.isdigit():
                        timestamp_format_invalid = False
                if timestamp_format_invalid:
                    error_string = 'error :: invalid %s value passed %s' % (key, value)
                    logger.error('error :: invalid %s value passed %s' % (key, value))
                    return 'Bad Request', 400

            if key == 'app':
                if value != 'all':
                    if value not in apps:
                        error_string = 'error :: no %s - %s' % (key, value)
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'source':
                if value != 'all':
                    if value not in sources:
                        error_string = 'error :: no %s - %s' % (key, value)
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'algorithm':
                if value != 'all':
                    if value not in algorithms:
                        error_string = 'error :: no %s - %s' % (key, value)
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'host':
                if value != 'all':
                    if value not in hosts:
                        error_string = 'error :: no %s - %s' % (key, value)
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

            if key == 'limit':
                limit_invalid = True
                limit_is_not_numeric = True
                if value.isdigit():
                    limit_is_not_numeric = False

                if limit_is_not_numeric:
                    error_string = 'error :: %s must be a numeric value - requested %s' % (key, value)
                    logger.error(error_string)
                    resp = json.dumps(
                        {'results': error_string})
                    return resp, 400

                new_value = int(value)
                try:
                    valid_value = new_value + 1
                except:
                    valid_value = None
                if valid_value and new_value < 101:
                    limit_invalid = False
                if limit_invalid:
                    error_string = 'error :: %s must be < 100 - requested %s' % (key, value)
                    logger.error(error_string)
                    resp = json.dumps(
                        {'results': error_string})
                    return resp, 400

            if key == 'order':
                order_invalid = True
                if value == 'DESC':
                    order_invalid = False
                if value == 'ASC':
                    order_invalid = False
                if order_invalid:
                    error_string = 'error :: %s must be DESC or ASC' % (key)
                    logger.error(error_string)
                    resp = json.dumps(
                        {'results': error_string})
                    return resp, 400

    # @added 20161127 - Branch #922: ionosphere
    if get_anomaly_id:
        try:
            query, panorama_data = panorama_request()
            logger.info('debug :: panorama_data - %s' % str(panorama_data))
        except:
            logger.error('error :: failed to get panorama: ' + traceback.format_exc())
            return 'Uh oh ... a Skyline 500 :(', 500

        try:
            duration = (time.time() - start)
            logger.info('debug :: duration - %s' % str(duration))
            resp = str(panorama_data)
            return resp, 200
        except:
            logger.error('error :: failed to render panorama.html: ' + traceback.format_exc())
            return 'Uh oh ... a Skyline 500 :(', 500

    if request_args_len == 0:
        try:
            panorama_data = panorama_request()
            # logger.info('panorama_data - %s' % str(panorama_data))
            return render_template(
                'panorama.html', anomalies=panorama_data, app_list=apps,
                source_list=sources, algorithm_list=algorithms,
                host_list=hosts, results='Latest anomalies',
                version=skyline_version, duration=(time.time() - start)), 200
        except:
            logger.error('error :: failed to get panorama: ' + traceback.format_exc())
            return 'Uh oh ... a Skyline 500 :(', 500
    else:
        count_request = 'false'
        if 'count_by_metric' in request.args:
            count_by_metric = request.args.get('count_by_metric', None)
            if count_by_metric == 'true':
                count_request = 'true'
        try:
            query, panorama_data = panorama_request()
            try:
                if settings.ENABLE_DEBUG or settings.ENABLE_WEBAPP_DEBUG:
                    logger.info('panorama_data - %s' % str(panorama_data))
                    logger.info('debug :: query - %s' % str(query))
                    logger.info('debug :: panorama_data - %s' % str(panorama_data))
                    logger.info('debug :: skyline_version - %s' % str(skyline_version))
            except:
                logger.error('error :: ENABLE_DEBUG or ENABLE_WEBAPP_DEBUG are not set in settings.py')
        except:
            logger.error('error :: failed to get panorama_request: ' + traceback.format_exc())
            return 'Uh oh ... a Skyline 500 :(', 500

        try:
            results_string = 'Found anomalies for %s' % str(query)

            duration = (time.time() - start)
            logger.info('debug :: duration - %s' % str(duration))
            return render_template(
                'panorama.html', anomalies=panorama_data, app_list=apps,
                source_list=sources, algorithm_list=algorithms,
                host_list=hosts, results=results_string, count_request=count_request,
                version=skyline_version, duration=(time.time() - start)), 200
        except:
            logger.error('error :: failed to render panorama.html: ' + traceback.format_exc())
            return 'Uh oh ... a Skyline 500 :(', 500


# Feature #1448: Crucible web UI - @earthgecko
# Branch #868: crucible - @earthgecko
# This may actually need Django, perhaps this is starting to move outside the
# realms of Flask..
@app.route("/crucible", methods=['GET'])
@requires_auth
def crucible():

    crucible_web_ui_implemented = False
    if crucible_web_ui_implemented:
        try:
            return render_template(
                'uh_oh.html', version=skyline_version,
                message="Sorry the Crucible web UI is not completed yet"), 200
        except:
            return render_template(
                'uh_oh.html', version=skyline_version,
                message="Sorry the Crucible web UI is not completed yet"), 200

# @added 20161123 - Branch #922: ionosphere


@app.route("/ionosphere", methods=['GET'])
@requires_auth
def ionosphere():
    if not settings.IONOSPHERE_ENABLED:
        try:
            return render_template(
                'uh_oh.html', version=skyline_version,
                message="Ionosphere is not enabled, please see the Ionosphere section in the docs and settings.py"), 200
        except:
            return 'Uh oh ... a Skyline 500 :(', 500

    start = time.time()

    request_args_present = False
    try:
        request_args_len = len(request.args)
        request_args_present = True
    except:
        request_args_len = 0

    IONOSPHERE_REQUEST_ARGS = [
        'timestamp', 'metric', 'metric_td', 'a_dated_list', 'timestamp_td',
        'requested_timestamp', 'fp_view', 'calc_features', 'add_fp',
        'features_profiles']

    determine_metric = False
    dated_list = False
    td_requested_timestamp = False
    feature_profile_view = False
    calculate_features = False
    create_feature_profile = False
    fp_view = False
    fp_profiles = []

    try:
        if request_args_present:
            timestamp_arg = False
            metric_arg = False
            metric_td_arg = False
            timestamp_td_arg = False

            if 'fp_view' in request.args:
                fp_view = request.args.get(str('fp_view'), None)

            for i in request.args:
                key = str(i)
                if key not in IONOSPHERE_REQUEST_ARGS:
                    logger.error('error :: invalid request argument - %s=%s' % (key, str(i)))
                    return 'Bad Request', 400
                value = request.args.get(key, None)
                logger.info('request argument - %s=%s' % (key, str(value)))

                if key == 'calc_features':
                    if str(value) == 'true':
                        calculate_features = True

                if key == 'add_fp':
                    if str(value) == 'true':
                        create_feature_profile = True

                if key == 'fp_view':
                    if str(value) == 'true':
                        fp_view = True

                if key == 'features_profiles':
                    fp_profiles = str(value)

                if key == 'a_dated_list':
                    if str(value) == 'true':
                        dated_list = True

                if key == 'requested_timestamp':
                    valid_rt_timestamp = False
                    if str(value) == 'False':
                        valid_rt_timestamp = True
                    if not valid_rt_timestamp:
                        if not len(str(value)) == 10:
                            logger.info('bad request argument - %s=%s not an epoch timestamp' % (str(key), str(value)))
                            resp = json.dumps(
                                {'results': 'Error: not an epoch timestamp for ' + str(key) + ' - ' + str(value) + ' - please pass a proper epoch timestamp'})
                        else:
                            try:
                                timestamp_numeric = int(value) + 1
                                valid_rt_timestamp = True
                            except:
                                valid_timestamp = False
                                logger.info('bad request argument - %s=%s not numeric' % (str(key), str(value)))
                                resp = json.dumps(
                                    {'results': 'Error: not a numeric epoch timestamp for ' + str(key) + ' - ' + str(value) + ' - please pass a proper epoch timestamp'})

                    if not valid_rt_timestamp:
                        return resp, 400

                if key == 'timestamp' or key == 'timestamp_td':
                    valid_timestamp = True
                    if not len(str(value)) == 10:
                        valid_timestamp = False
                        logger.info('bad request argument - %s=%s not an epoch timestamp' % (str(key), str(value)))
                        resp = json.dumps(
                            {'results': 'Error: not an epoch timestamp for ' + str(key) + ' - ' + str(value) + ' - please pass a proper epoch timestamp'})
                    if valid_timestamp:
                        try:
                            timestamp_numeric = int(value) + 1
                        except:
                            valid_timestamp = False
                            logger.info('bad request argument - %s=%s not numeric' % (str(key), str(value)))
                            resp = json.dumps(
                                {'results': 'Error: not a numeric epoch timestamp for ' + str(key) + ' - ' + str(value) + ' - please pass a proper epoch timestamp'})

                    if not valid_timestamp:
                        return resp, 400

                    if not fp_view:
                        ionosphere_data_dir = '%s/%s' % (settings.IONOSPHERE_DATA_FOLDER, str(value))
                        if not isdir(ionosphere_data_dir):
                            valid_timestamp = False
                            now = time.time()
                            purged_timestamp = int(now) - int(settings.IONOSPHERE_KEEP_TRAINING_TIMESERIES_FOR)
                            if int(value) < purged_timestamp:
                                logger.info('%s=%s timestamp it to old to have training data' % (key, str(value)))
                                resp = json.dumps(
                                    {'results': 'Error: timestamp too old no training data exists, training data has been purged'})
                            else:
                                logger.error('%s=%s no timestamp training data dir found - %s' % (key, str(value), ionosphere_data_dir))
                                resp = json.dumps(
                                    {'results': 'Error: no training data dir exists - ' + ionosphere_data_dir + ' - go on... nothing here.'})

                    if not valid_timestamp:
                        return resp, 404

                if key == 'timestamp':
                    requested_timestamp = str(value)
                    timestamp_arg = True

                if key == 'timestamp_td':
                    timestamp_td_arg = True
                    requested_timestamp_td = str(value)
                    # determine_metric = True

                if key == 'requested_timestamp':
                    td_requested_timestamp = str(value)

                if key == 'metric' or key == 'metric_td':
                    try:
                        unique_metrics = list(REDIS_CONN.smembers(settings.FULL_NAMESPACE + 'unique_metrics'))
                    except:
                        logger.error('error :: Webapp could not get the unique_metrics list from Redis')
                        logger.info(traceback.format_exc())
                        return 'Internal Server Error', 500
                    metric_name = settings.FULL_NAMESPACE + str(value)
                    if metric_name not in unique_metrics:
                        error_string = 'error :: no metric - %s - exists in Redis' % metric_name
                        logger.error(error_string)
                        resp = json.dumps(
                            {'results': error_string})
                        return resp, 404

                if key == 'metric':
                    metric_arg = True

                if key == 'metric_td':
                    metric_td_arg = True

                if metric_arg or metric_td_arg:
                    if key == 'metric' or key == 'metric_td':
                        base_name = str(value)

                if timestamp_arg and metric_arg:
                    timeseries_dir = base_name.replace('.', '/')

                    if not fp_view:
                        ionosphere_data_dir = '%s/%s/%s' % (
                            settings.IONOSPHERE_DATA_FOLDER,
                            requested_timestamp, timeseries_dir)

                        if not isdir(ionosphere_data_dir):
                            logger.info(
                                '%s=%s no timestamp metric training data dir found - %s' %
                                (key, str(value), ionosphere_data_dir))
                            resp = json.dumps(
                                {'results': 'Error: no training data dir exists - ' + ionosphere_data_dir + ' - go on... nothing here.'})
                            return resp, 404
                    else:
                        ionosphere_profiles_dir = '%s/%s/%s' % (
                            settings.IONOSPHERE_PROFILES_FOLDER, timeseries_dir,
                            requested_timestamp, )

                        if not isdir(ionosphere_profiles_dir):
                            logger.info(
                                '%s=%s no timestamp metric features profile dir found - %s' %
                                (key, str(value), ionosphere_profiles_dir))
                            resp = json.dumps(
                                {'results': 'Error: no features profile dir exists - ' + ionosphere_profiles_dir + ' - go on... nothing here.'})
                            return resp, 404

        logger.debug('arguments validated - OK')

    except:
        message = 'Uh oh ... a Skyline 500 :('
        trace = traceback.format_exc()
        return internal_error(message, trace)

    debug_on = False

    if fp_view:
        context = 'features_profiles'
    else:
        context = 'training_data'
    fp_view_on = fp_view

    do_first = False
    if fp_view:
        do_first = True
        args = [
            'timestamp', 'metric', 'metric_td', 'timestamp_td',
            'requested_timestamp', 'features_profiles']
        for i_arg in args:
            if i_arg in request.args:
                do_first = False

    if request_args_len == 0 or dated_list:
        do_first = True

    if do_first:
        listed_by = 'metric'
        if dated_list:
            listed_by = 'date'
        try:
            mpaths, unique_m, unique_ts, hdates = ionosphere_data(False, 'all', context)
            return render_template(
                'ionosphere.html', unique_metrics=unique_m, list_by=listed_by,
                unique_timestamps=unique_ts, human_dates=hdates,
                metric_td_dirs=zip(unique_ts, hdates), td_files=mpaths,
                requested_timestamp=td_requested_timestamp, fp_view=fp_view_on,
                version=skyline_version, duration=(time.time() - start),
                print_debug=debug_on), 200
        except:
            message = 'Uh oh ... a Skyline 500 :('
            trace = traceback.format_exc()
            return internal_error(message, trace)

    if metric_td_arg:
        listed_by = 'metric_td_dirs'
        try:
            mpaths, unique_m, unique_ts, hdates = ionosphere_data(False, base_name, context)
            return render_template(
                'ionosphere.html', metric_td_dirs=zip(unique_ts, hdates),
                list_by=listed_by, for_metric=base_name, td_files=mpaths,
                requested_timestamp=td_requested_timestamp, fp_view=fp_view_on,
                version=skyline_version, duration=(time.time() - start),
                print_debug=debug_on), 200
        except:
            message = 'Uh oh ... a Skyline 500 :('
            trace = traceback.format_exc()
            return internal_error(message, trace)

    if timestamp_td_arg:
        # Note to self.  Can we carry the referring timestamp through and when
        # a metric is selected the time is the only one wrapped in <code> .e.g red?
        listed_by = 'timestamp_td_dirs'
        try:
            mpaths, unique_m, unique_ts, hdates = ionosphere_get_metrics_dir(requested_timestamp_td, context)
            return render_template(
                'ionosphere.html', unique_metrics=unique_m, list_by=listed_by,
                unique_timestamps=unique_ts, human_dates=hdates, td_files=mpaths,
                metric_td_dirs=zip(unique_ts, hdates),
                requested_timestamp=td_requested_timestamp, fp_view=fp_view_on,
                version=skyline_version, duration=(time.time() - start),
                print_debug=debug_on), 200
        except:
            message = 'Uh oh ... a Skyline 500 :('
            trace = traceback.format_exc()
            return internal_error(message, trace)

    if timestamp_arg and metric_arg:
        try:
            graph_url = '%scactiStyle(%s)%s&colorList=blue' % (
                settings.GRAPH_URL, base_name, settings.GRAPHITE_GRAPH_SETTINGS)
        except:
            graph_url = False

        features = None
        f_calc = 'none'
        fp_exists = False
        fp_id = None

        if calculate_features or create_feature_profile or fp_view:
            try:
                fp_csv, successful, fp_exists, fp_id, fail_msg, traceback_format_exc, f_calc = calculate_features_profile(skyline_app, requested_timestamp, base_name, context)
            except:
                trace = traceback.format_exc()
                message = 'failed to calculate features'
                return internal_error(message, trace)

            if not successful:
                return internal_error(fail_msg, traceback_format_exc)
            if os.path.isfile(str(fp_csv)):
                features = []
                with open(fp_csv, 'rb') as fr:
                    reader = csv.reader(fr, delimiter=',')
                    for i, line in enumerate(reader):
                        features.append([str(line[0]), str(line[1])])

        if create_feature_profile or fp_view:
            if create_feature_profile:
                # Submit to Ionosphere to run tsfresh on
                create_feature_profile = True
            if not fp_id:
                try:
                    fp_id, fp_in_successful, fp_exists, fail_msg, traceback_format_exc = create_features_profile(requested_timestamp, base_name, context)
                except:
                    # @modified 20161209 -  - Branch #922: ionosphere
                    #                        Task #1658: Patterning Skyline Ionosphere
                    # Use raise and traceback.format_exc() to carry
                    # ionosphere_backend.py through to the rendered page for the user, e.g
                    # me.
                    # trace = traceback_format_exc
                    trace = traceback.format_exc()
                    message = 'failed to create features profile'
                    return internal_error(message, trace)
                if not fp_in_successful:
                    trace = traceback.format_exc()
                    fail_msg = 'error :: create_features_profile failed'
                    return internal_error(fail_msg, 'no traceback available')

        fp_details = None
        if fp_view:
            try:
                fp_details, fp_details_successful, fail_msg, traceback_format_exc = features_profile_details(fp_id)
            except:
                # trace = traceback_format_exc
                trace = traceback.format_exc()
                message = 'failed to get features profile details'
                return internal_error(message, trace)
            if not fp_details_successful:
                trace = traceback.format_exc()
                fail_msg = 'error :: features_profile_details failed'
                return internal_error(fail_msg, trace)

        try:
            mpaths, images, hdate, m_vars, ts_json, data_to_process, p_id = ionosphere_metric_data(requested_timestamp, base_name, context)
            return render_template(
                'ionosphere.html', timestamp=requested_timestamp,
                for_metric=base_name, metric_vars=m_vars, metric_files=mpaths,
                metric_images=images, human_date=hdate, timeseries=ts_json,
                data_ok=data_to_process, td_files=mpaths,
                panorama_anomaly_id=p_id, graphite_url=graph_url,
                extracted_features=features, calc_time=f_calc,
                features_profile_id=fp_id, features_profile_exists=fp_exists,
                fp_view=fp_view_on, features_profile_details=fp_details,
                tsfresh_version=TSFRESH_VERSION,
                version=skyline_version, duration=(time.time() - start),
                print_debug=debug_on), 200
        except:
            message = 'Uh oh ... a Skyline 500 :('
            trace = traceback.format_exc()
            return internal_error(message, trace)

    try:
        message = 'Unknown request'
        return render_template(
            'ionosphere.html', display_message=message,
            version=skyline_version, duration=(time.time() - start),
            print_debug=debug_on), 200
    except:
        message = 'Uh oh ... a Skyline 500 :('
        trace = traceback.format_exc()
        return internal_error(message, trace)


@app.route('/ionosphere_images')
def ionosphere_images():

    request_args_present = False
    try:
        request_args_len = len(request.args)
        request_args_present = True
    except:
        request_args_len = 0

    IONOSPHERE_REQUEST_ARGS = ['image']

    if request_args_present:
        for i in request.args:
            key = str(i)
            if key not in IONOSPHERE_REQUEST_ARGS:
                logger.error('error :: invalid request argument - %s=%s' % (key, str(i)))
                return 'Bad Request', 400
            value = request.args.get(key, None)
            logger.info('request argument - %s=%s' % (key, str(value)))

            if key == 'image':
                filename = str(value)
                if os.path.isfile(filename):
                    try:
                        return send_file(filename, mimetype='image/png')
                    except:
                        message = 'Uh oh ... a Skyline 500 :( - could not return %s' % filename
                        trace = traceback.format_exc()
                        return internal_error(message, trace)
                else:
                    image_404_path = 'webapp/static/images/skyline.ionosphere.image.404.png'
                    filename = path.abspath(
                        path.join(path.dirname(__file__), '..', image_404_path))
                    try:
                        return send_file(filename, mimetype='image/png')
                    except:
                        message = 'Uh oh ... a Skyline 500 :( - could not return %s' % filename
                        trace = traceback.format_exc()
                        return internal_error(message, trace)

    return 'Bad Request', 400

# @added 20160703 - Feature #1464: Webapp Redis browser
# A port of Marian Steinbach's rebrow - https://github.com/marians/rebrow
# Description of info keys
# TODO: to be continued.
serverinfo_meta = {
    'aof_current_rewrite_time_sec': "Duration of the on-going <abbr title='Append-Only File'>AOF</abbr> rewrite operation if any",
    'aof_enabled': "Flag indicating <abbr title='Append-Only File'>AOF</abbr> logging is activated",
    'aof_last_bgrewrite_status': "Status of the last <abbr title='Append-Only File'>AOF</abbr> rewrite operation",
    'aof_last_rewrite_time_sec': "Duration of the last <abbr title='Append-Only File'>AOF</abbr> rewrite operation in seconds",
    'aof_last_write_status': "Status of last <abbr title='Append-Only File'>AOF</abbr> write operation",
    'aof_rewrite_in_progress': "Flag indicating a <abbr title='Append-Only File'>AOF</abbr> rewrite operation is on-going",
    'aof_rewrite_scheduled': "Flag indicating an <abbr title='Append-Only File'>AOF</abbr> rewrite operation will be scheduled once the on-going RDB save is complete",
    'arch_bits': 'Architecture (32 or 64 bits)',
    'blocked_clients': 'Number of clients pending on a blocking call (BLPOP, BRPOP, BRPOPLPUSH)',
    'client_biggest_input_buf': 'biggest input buffer among current client connections',
    'client_longest_output_list': None,
    'cmdstat_client': 'Statistics for the client command',
    'cmdstat_config': 'Statistics for the config command',
    'cmdstat_dbsize': 'Statistics for the dbsize command',
    'cmdstat_del': 'Statistics for the del command',
    'cmdstat_dump': 'Statistics for the dump command',
    'cmdstat_expire': 'Statistics for the expire command',
    'cmdstat_flushall': 'Statistics for the flushall command',
    'cmdstat_get': 'Statistics for the get command',
    'cmdstat_hgetall': 'Statistics for the hgetall command',
    'cmdstat_hkeys': 'Statistics for the hkeys command',
    'cmdstat_hmset': 'Statistics for the hmset command',
    'cmdstat_info': 'Statistics for the info command',
    'cmdstat_keys': 'Statistics for the keys command',
    'cmdstat_llen': 'Statistics for the llen command',
    'cmdstat_ping': 'Statistics for the ping command',
    'cmdstat_psubscribe': 'Statistics for the psubscribe command',
    'cmdstat_pttl': 'Statistics for the pttl command',
    'cmdstat_sadd': 'Statistics for the sadd command',
    'cmdstat_scan': 'Statistics for the scan command',
    'cmdstat_select': 'Statistics for the select command',
    'cmdstat_set': 'Statistics for the set command',
    'cmdstat_smembers': 'Statistics for the smembers command',
    'cmdstat_sscan': 'Statistics for the sscan command',
    'cmdstat_ttl': 'Statistics for the ttl command',
    'cmdstat_type': 'Statistics for the type command',
    'cmdstat_zadd': 'Statistics for the zadd command',
    'cmdstat_zcard': 'Statistics for the zcard command',
    'cmdstat_zrange': 'Statistics for the zrange command',
    'cmdstat_zremrangebyrank': 'Statistics for the zremrangebyrank command',
    'cmdstat_zrevrange': 'Statistics for the zrevrange command',
    'cmdstat_zscan': 'Statistics for the zscan command',
    'config_file': None,
    'connected_clients': None,
    'connected_slaves': None,
    'db0': None,
    'evicted_keys': None,
    'expired_keys': None,
    'gcc_version': None,
    'hz': None,
    'instantaneous_ops_per_sec': None,
    'keyspace_hits': None,
    'keyspace_misses': None,
    'latest_fork_usec': None,
    'loading': None,
    'lru_clock': None,
    'master_repl_offset': None,
    'mem_allocator': None,
    'mem_fragmentation_ratio': None,
    'multiplexing_api': None,
    'os': None,
    'process_id': None,
    'pubsub_channels': None,
    'pubsub_patterns': None,
    'rdb_bgsave_in_progress': None,
    'rdb_changes_since_last_save': None,
    'rdb_current_bgsave_time_sec': None,
    'rdb_last_bgsave_status': None,
    'rdb_last_bgsave_time_sec': None,
    'rdb_last_save_time': None,
    'redis_build_id': None,
    'redis_git_dirty': None,
    'redis_git_sha1': None,
    'redis_mode': None,
    'redis_version': None,
    'rejected_connections': None,
    'repl_backlog_active': None,
    'repl_backlog_first_byte_offset': None,
    'repl_backlog_histlen': None,
    'repl_backlog_size': None,
    'role': None,
    'run_id': None,
    'sync_full': None,
    'sync_partial_err': None,
    'sync_partial_ok': None,
    'tcp_port': None,
    'total_commands_processed': None,
    'total_connections_received': None,
    'uptime_in_days': None,
    'uptime_in_seconds': None,
    'used_cpu_sys': None,
    'used_cpu_sys_children': None,
    'used_cpu_user': None,
    'used_cpu_user_children': None,
    'used_memory': None,
    'used_memory_human': None,
    'used_memory_lua': None,
    'used_memory_peak': None,
    'used_memory_peak_human': None,
    'used_memory_rss': None
}


@app.route('/rebrow', methods=['GET', 'POST'])
# def login():
def rebrow():
    """
    Start page
    """
    if request.method == 'POST':
        # TODO: test connection, handle failures
        host = request.form['host']
        port = int(request.form['port'])
        db = int(request.form['db'])
        url = url_for('rebrow_server_db', host=host, port=port, db=db)
        return redirect(url)
    else:
        start = time.time()
        return render_template(
            'rebrow_login.html',
            version=skyline_version,
            duration=(time.time() - start))


@app.route("/rebrow_server_db/<host>:<int:port>/<int:db>/")
def rebrow_server_db(host, port, db):
    """
    List all databases and show info on server
    """
    start = time.time()
    r = redis.StrictRedis(host=host, port=port, db=0)
    info = r.info('all')
    dbsize = r.dbsize()
    return render_template(
        'rebrow_server_db.html',
        host=host,
        port=port,
        db=db,
        info=info,
        dbsize=dbsize,
        serverinfo_meta=serverinfo_meta,
        version=skyline_version,
        duration=(time.time() - start))


@app.route("/rebrow_keys/<host>:<int:port>/<int:db>/keys/", methods=['GET', 'POST'])
def rebrow_keys(host, port, db):
    """
    List keys for one database
    """
    start = time.time()
    r = redis.StrictRedis(host=host, port=port, db=db)
    if request.method == 'POST':
        action = request.form['action']
        app.logger.debug(action)
        if action == 'delkey':
            if request.form['key'] is not None:
                result = r.delete(request.form['key'])
                if result == 1:
                    flash('Key %s has been deleted.' % request.form['key'], category='info')
                else:
                    flash('Key %s could not be deleted.' % request.form['key'], category='error')
        return redirect(request.url)
    else:
        offset = int(request.args.get('offset', '0'))
        perpage = int(request.args.get('perpage', '10'))
        pattern = request.args.get('pattern', '*')
        dbsize = r.dbsize()
        keys = sorted(r.keys(pattern))
        limited_keys = keys[offset:(perpage + offset)]
        types = {}
        for key in limited_keys:
            types[key] = r.type(key)
        return render_template(
            'rebrow_keys.html',
            host=host,
            port=port,
            db=db,
            dbsize=dbsize,
            keys=limited_keys,
            types=types,
            offset=offset,
            perpage=perpage,
            pattern=pattern,
            num_keys=len(keys),
            version=skyline_version,
            duration=(time.time() - start))


@app.route("/rebrow_key/<host>:<int:port>/<int:db>/keys/<key>/")
def rebrow_key(host, port, db, key):
    """
    Show a specific key.
    key is expected to be URL-safe base64 encoded
    """
# @added 20160703 - Feature #1464: Webapp Redis browser
# metrics encoded with msgpack
    original_key = key
    msg_pack_key = False
    # if key.startswith('metrics.'):
    #     msg_packed_key = True
    key = base64.urlsafe_b64decode(key.encode('utf8'))
    start = time.time()
    r = redis.StrictRedis(host=host, port=port, db=db)
    dump = r.dump(key)
    if dump is None:
        abort(404)
    # if t is None:
    #    abort(404)
    size = len(dump)
    del dump
    t = r.type(key)
    ttl = r.pttl(key)
    if t == 'string':
        # @modified 20160703 - Feature #1464: Webapp Redis browser
        # metrics encoded with msgpack
        # val = r.get(key)
        try:
            val = r.get(key)
        except:
            abort(404)
        test_string = all(c in string.printable for c in val)
        if not test_string:
            raw_result = r.get(key)
            unpacker = Unpacker(use_list=False)
            unpacker.feed(raw_result)
            val = list(unpacker)
            msg_pack_key = True
    elif t == 'list':
        val = r.lrange(key, 0, -1)
    elif t == 'hash':
        val = r.hgetall(key)
    elif t == 'set':
        val = r.smembers(key)
    elif t == 'zset':
        val = r.zrange(key, 0, -1, withscores=True)
    return render_template(
        'rebrow_key.html',
        host=host,
        port=port,
        db=db,
        key=key,
        value=val,
        type=t,
        size=size,
        ttl=ttl / 1000.0,
        now=datetime.utcnow(),
        expiration=datetime.utcnow() + timedelta(seconds=ttl / 1000.0),
        version=skyline_version,
        duration=(time.time() - start),
        msg_packed_key=msg_pack_key)


@app.template_filter('urlsafe_base64')
def urlsafe_base64_encode(s):
    if type(s) == 'Markup':
        s = s.unescape()
    s = s.encode('utf8')
    s = base64.urlsafe_b64encode(s)
    return Markup(s)
# END rebrow


class App():
    def __init__(self):
        self.stdin_path = '/dev/null'
        self.stdout_path = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
        self.stderr_path = '%s/%s.log' % (settings.LOG_PATH, skyline_app)
        self.pidfile_path = '%s/%s.pid' % (settings.PID_PATH, skyline_app)
        self.pidfile_timeout = 5

    def run(self):

        # Log management to prevent overwriting
        # Allow the bin/<skyline_app>.d to manage the log
        if os.path.isfile(skyline_app_logwait):
            try:
                os_remove(skyline_app_logwait)
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_logwait)
                pass

        now = time()
#        log_wait_for = now + 5
        log_wait_for = now + 1
        while now < log_wait_for:
            if os.path.isfile(skyline_app_loglock):
                sleep(.1)
                now = time()
            else:
                now = log_wait_for + 1

        logger.info('starting %s run' % skyline_app)
        if os.path.isfile(skyline_app_loglock):
            logger.error('error - bin/%s.d log management seems to have failed, continuing' % skyline_app)
            try:
                os_remove(skyline_app_loglock)
                logger.info('log lock file removed')
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_loglock)
                pass
        else:
            logger.info('bin/%s.d log management done' % skyline_app)

        try:
            logger.info('starting %s - %s' % (skyline_app, skyline_version))
        except:
            logger.info('starting %s - version UNKNOWN' % (skyline_app))
        logger.info('hosted at %s' % settings.WEBAPP_IP)
        logger.info('running on port %d' % settings.WEBAPP_PORT)

        app.run(settings.WEBAPP_IP, settings.WEBAPP_PORT)


def run():
    """
    Start the Webapp server
    """
    if not isdir(settings.PID_PATH):
        print ('pid directory does not exist at %s' % settings.PID_PATH)
        sys.exit(1)

    if not isdir(settings.LOG_PATH):
        print ('log directory does not exist at %s' % settings.LOG_PATH)
        sys.exit(1)

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s :: %(process)s :: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.handlers.TimedRotatingFileHandler(
        logfile,
        when="midnight",
        interval=1,
        backupCount=5)

    memory_handler = logging.handlers.MemoryHandler(100,
                                                    flushLevel=logging.DEBUG,
                                                    target=handler)
    handler.setFormatter(formatter)
    logger.addHandler(memory_handler)

    # Validate settings variables
    valid_settings = validate_settings_variables(skyline_app)

    if not valid_settings:
        print ('error :: invalid variables in settings.py - cannot start')
        sys.exit(1)

    try:
        settings.WEBAPP_SERVER
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_SERVER'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_SERVER'))
        sys.exit(1)
    try:
        settings.WEBAPP_IP
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_IP'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_IP'))
        sys.exit(1)
    try:
        settings.WEBAPP_PORT
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_PORT'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_PORT'))
        sys.exit(1)
    try:
        settings.WEBAPP_AUTH_ENABLED
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_AUTH_ENABLED'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_AUTH_ENABLED'))
        sys.exit(1)
    try:
        settings.WEBAPP_IP_RESTRICTED
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_IP_RESTRICTED'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_IP_RESTRICTED'))
        sys.exit(1)
    try:
        settings.WEBAPP_AUTH_USER
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_AUTH_USER'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_AUTH_USER'))
        sys.exit(1)
    try:
        settings.WEBAPP_AUTH_USER_PASSWORD
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_AUTH_USER_PASSWORD'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_AUTH_USER_PASSWORD'))
        sys.exit(1)
    try:
        settings.WEBAPP_ALLOWED_IPS
    except:
        logger.error('error :: failed to determine %s from settings.py' % str('WEBAPP_ALLOWED_IPS'))
        print ('Failed to determine %s from settings.py' % str('WEBAPP_ALLOWED_IPS'))
        sys.exit(1)

    webapp = App()

# Does this make it log?
#    if len(sys.argv) > 1 and sys.argv[1] == 'run':
#        webapp.run()
#    else:
#        daemon_runner = runner.DaemonRunner(webapp)
#        daemon_runner.daemon_context.files_preserve = [handler.stream]
#        daemon_runner.do_action()

    daemon_runner = runner.DaemonRunner(webapp)
    daemon_runner.daemon_context.files_preserve = [handler.stream]
    daemon_runner.do_action()

if __name__ == "__main__":
    run()
