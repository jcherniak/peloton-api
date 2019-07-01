#! /usr/bin/env python3.6
# -*- coding: latin-1 -*-

import os
import requests
import logging
import decimal

from datetime import datetime
from datetime import timezone
from datetime import date
from .version import __version__

# Set our base URL location
_BASE_URL = 'https://api.onepeloton.com'

# Being friendly, let Peloton know who we are (eg: not the web ui)
_USER_AGENT = "peloton-client-library/{}".format(__version__)

def get_logger():
    """ To change log level from calling code, use something like
        logging.getLogger("peloton").setLevel(logging.DEBUG)
    """
    logger = logging.getLogger("peloton")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


SHOW_WARNINGS = False

try:

    # Mandatory credentials
    PELOTON_USERNAME = os.environ['PELOTON_USER']
    PELOTON_PASSWORD = os.environ['PELOTON_PASSWORD']

    # Additional option to show or hide warnings
    try:
        ignore_warnings = os.environ['PELOTON_IGNORE_WARNINGS']
        SHOW_WARNINGS = False if ignore_warnings else True

    except:
        SHOW_WARNINGS = False

    if SHOW_WARNINGS:
        get_logger().setLevel(logging.WARNING)
    else:
        get_logger().setLevel(logging.ERROR)

    # Whether or not to verify SSL connections (defaults to True)
    try:
        SSL_VERIFY = os.environ['PELOTON_SSL_VERIFY']
    except:
        SSL_VERIFY = True

    # If set, we'll use this cert to verify against. Useful when you're stuck behind SSL MITM
    try:
        SSL_CERT = os.environ['PELOTON_SSL_CERT']
    except:
        SSL_CERT = None

except Exception as e:
    get_logger().error("No `username` or `password` environment variable set\n"
                         "Please ensure you specify one prior to utilizing the API\n")

class NotLoaded:
    """ In an effort to avoid pissing Peloton off, we lazy load as often as possible. This class
    is utitilzed frequently within this module to indicate when data can be retrieved, as requested"""
    pass


class DataMissing:
    """ Used to indicate that data is missing (eg: not h/r monitor used)
    """
    pass


class PelotonException(Exception):
    """ This is our base exception class, that all other exceptions inherit from
    """
    pass


class PelotonClientError(PelotonException):
    """ Client exception class
    """
    def __init__(self, message, response):
        super(PelotonException, self).__init__(self, message)
        self.message = message
        self.response = response


class PelotonServerError(PelotonException):
    """ Server exception class
    """
    def __init__(self, message, response):
        super(PelotonException, self).__init__(self, message)
        self.message = message
        self.response = response


class PelotonRedirectError(PelotonException):
    """ Maybe we'll see weird unexpected redirects?
    """
    def __init__(self, message, response):
        super(PelotonException, self).__init__(self, message)
        self.message = message
        self.response = response


class PelotonObject:
    """ Base class for all Peloton data
    """

    def serialize(self, depth=1, load_all=True):
        """Ensures that everything has a .serialize() method so that all data is serializable

        Args:
            depth: level of nesting to include when serializing
            load_all: whether or not to include lazy loaded data (eg: NotLoaded() instances)
        """

        # Dict to hold our returnable data
        ret = {}

        # Dict to hold the attributes of $.this object
        obj_attrs = {}

        # If we hit our depth limit, return
        if depth == 0:
            return None

        # List of keys that we will not be included in our serailizable output based on load_all
        dont_load = []

        # Load our NotLoaded() (lazy loading) instances if we're requesting to do so
        for k in self.__dict__:
            if load_all:
                obj_attrs[k] = getattr(self, k)
                continue

            # Don't include lazy loaded attrs
            raw_value = super(PelotonObject, self).__getattribute__(k)
            if isinstance(raw_value, NotLoaded):
                dont_load.append(k)

        # We've gone through our pre-flight prep, now lets actually serailize our data
        for k, v in obj_attrs.items():

            # Ignore this key if it's in our dont_load list or is private
            if k.startswith('_') or k in dont_load:
                continue

            if isinstance(v, PelotonObject):
                if depth > 1:
                    ret[k] = v.serialize(depth=depth - 1)

            elif isinstance(v, list):
                serialized_list = []

                for val in v:
                    if isinstance(val, PelotonObject):
                        if depth > 1:
                            serialized_list.append(val.serialize(depth=depth - 1))

                    elif isinstance(val, (datetime, date)):
                        serialized_list.append(val.isoformat())

                    elif isinstance(val, decimal.Decimal):
                        serialized_list.append("%.1f" % val)

                    elif isinstance(val, (str, int, dict)):
                        serialized_list.append(val)

                # Only add if we have data (this _can_ be an empty list in the event that our list is noting but
                #   PelotonObject's and we're at/past our recursion depth)
                if serialized_list:
                    ret[k] = serialized_list

                # If v is empty, return an empty list
                elif not v:
                    ret[k] = []

            else:
                if isinstance(v, (datetime, date)):
                    ret[k] = v.isoformat()

                elif isinstance(v, decimal.Decimal):
                    ret[k] = "%.1f" % v

                else:
                    ret[k] = v

        # We've got a python dict now, so lets return it
        return ret


class PelotonAPI:
    """ Base class that factory classes within this module inherit from.
    This class is _not_ meant to be utilized directly, so don't do it.

    Core "working" class of the Peolton API Module
    """

    peloton_username = None
    peloton_password = None

    # Hold a request.Session instance that we're going to rely on to make API calls
    peloton_session = None

    # Being friendly (by default), use the same page size that the Peloton website uses
    page_size = 10

    # Hold our user ID (pulled when we authenticate to the API)
    user_id = None

    # Headers we'll be using for each request
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT
    }

    @classmethod
    def _api_request(cls, uri, params={}):
        """ Base function that everything will use under the hood to interact with the API

        Returns a requests response instance, or raises an exception on error
        """

        # Create a session if we don't have one yet
        if cls.peloton_session is None:
            cls._create_api_session()

        get_logger().debug("Request {} [{}]".format(_BASE_URL + uri, params))
        resp = cls.peloton_session.get(_BASE_URL + uri, headers=cls.headers, params=params)
        get_logger().debug("Response {}: [{}]".format(resp.status_code, resp._content))

        # If we don't have a 200 code
        if not (200 >= resp.status_code < 300):

            message = resp._contnet

            if 300 <= resp.status_code < 400:
                raise PelotonRedirectError("Unexpected Redirect", resp)

            elif 400 <= resp.status_code < 500:
                raise PelotonClientError(message, resp)

            elif 500 <= resp.status_code < 600:
                raise PelotonServerError(message, resp)

        return resp

    @classmethod
    def _create_api_session(cls):
        """ Create a session instance for communicating with the API
        """

        if cls.peloton_username is None:
            cls.peloton_username = PELOTON_USERNAME

        if cls.peloton_password is None:
            cls.peloton_password = PELOTON_PASSWORD

        if cls.peloton_username is None or cls.peloton_password is None:
            raise PelotonClientError("The Peloton Client Library requires a `username` and `password` be set in "
                                     "`/.config/peloton, under section `peloton`")

        payload = {
            'username_or_email': cls.peloton_username,
            'password': cls.peloton_password
        }

        cls.peloton_session = requests.Session()
        resp = cls.peloton_session.post(_BASE_URL + '/auth/login', json=payload, headers=cls.headers)
        message = resp._content

        if 300 <= resp.status_code < 400:
            raise PelotonRedirectError("Unexpected Redirect", resp)

        elif 400 <= resp.status_code < 500:
            raise PelotonClientError(message, resp)

        elif 500 <= resp.status_code < 600:
            raise PelotonServerError(message, resp)

        # Set our User ID on our class
        cls.user_id = resp.json()['user_id']


class PelotonUser(PelotonObject):
    """ Read-Only class that describes a Peloton User

    This class should never be invoked directly

    TODO: Flesh this out
    """

    def __init__(self, **kwargs):

        self.username = kwargs.get('username')
        self.id = kwargs.get('id')

    def __str__(self):
        return self.username


class PelotonWorkout(PelotonObject):
    """ A read-only class that defines a workout instance/object

    This class should never be instantiated directly!
    """

    def __init__(self, **kwargs):
        """ This class is instantiated by
        PelotonWorkout.get()
        PelotonWorkout.list()
        """

        self.id = kwargs.get('id')

        # This is a bit weird, we can only get ride details if they come up from a users workout list via a join
        self.ride = NotLoaded()
        if kwargs.get('ride') is not None:
            self.ride = PelotonRide(**kwargs.get('ride'))

        # Hard-stop on metrics, we'll populate that below
        self.metrics = NotLoaded()

        # List of achievements that were obtained during this workout
        self.achievements = NotLoaded()
        achievements = kwargs.get('achievement_templates')
        if achievements is not None:
            self.achievements = []
            for achievement in achievements:
                self.achievements.append((achievement['name'], achievement['description']))

        # Not entirely certain what the difference is between these two fields
        self.created = datetime.fromtimestamp(kwargs.get('created', 0), timezone.utc)
        self.created_at = datetime.fromtimestamp(kwargs.get('created_at', 0), timezone.utc)

        # Time duration of this ride
        self.start_time = datetime.fromtimestamp(kwargs.get('start_time', 0), timezone.utc)
        self.end_time = datetime.fromtimestamp(kwargs.get('end_time', 0), timezone.utc)

        # What exercise type is this?
        self.fitness_discipline = kwargs.get('fitness_discipline')

        # Basic leaderboard stats
        #   Need to call /api/workout/workout_id to get these two bits
        self.leaderboard_rank = kwargs.get('leaderboard_rank', NotLoaded())
        self.leaderboard_users = kwargs.get('total_leaderboard_users', NotLoaded())

        # Workout status (complete, in progress, etc)
        self.status = kwargs.get('status')

    def __str__(self):
        return self.fitness_discipline

    def __getattribute__(self, attr):

        value = object.__getattribute__(self, attr)

        # Handle accessing NotLoaded attributes (yay lazy loading)
        #   TODO: Handle ride laoding if its NotLoaded()
        if attr in ['metrics', 'leaderboard_rank', 'leaderboard_users', 'achievements'] and type(value) is NotLoaded:

            if attr == 'metrics':
                metrics = PelotonWorkoutMetricsFactory.get(self.id)
                self.metrics = metrics
                return metrics

            elif attr.startswith('leaderboard_') or attr == 'achievements':
                workout = PelotonWorkoutFactory.get(self.id)

                # Load leaderboard stats
                self.leaderboard_rank = workout.leaderboard_rank
                self.leaderboard_users = workout.leaderboard_users

                # Load our achievements
                self.achievements = workout.achievements

                # Return the value of the requested attribute
                return getattr(self, attr)

        # Return our default object (eg: NotLoaded()) if our requested attribute isn't expected
        return value

    @classmethod
    def get(cls, workout_id):
        """ Get a specific workout
        """
        raise NotImplementedError()


    @classmethod
    def list(cls):
        """ Return a list of all workouts
        """
        return PelotonWorkoutFactory.list()


class PelotonRide(PelotonObject):
    """ A read-only class that defines a ride (workout class)

    This class should never be invoked directly!
    """

    def __init__(self, **kwargs):

        self.title = kwargs.get('title')
        self.id = kwargs.get('id')
        self.description = kwargs.get('description')
        self.duration = kwargs.get('duration')
        self.instructor_id = kwargs.get('instructor_id')

    def __str__(self):
        return self.title

    @classmethod
    def get(cls, ride_id):
        raise NotImplementedError()


class PelotonMetric(PelotonObject):
    """ A read-only class that outlines some simple metric information about the workout
    """

    def __init__(self, **kwargs):

        self.values = kwargs.get('values')
        self.average = kwargs.get('average_value')
        self.name = kwargs.get('display_name')
        self.unit = kwargs.get('display_unit')
        self.max = kwargs.get('max_value')
        self.slug = kwargs.get('slug')

    def __str__(self):
        return self.name


class PelotonMetricSummary(PelotonObject):
    """ An object that describes a summary of a metric set
    """

    def __init__(self, **kwargs):

        self.name = kwargs.get('display_name')
        self.value = kwargs.get('value')
        self.unit = kwargs.get('display_unit')
        self.slug = kwargs.get('slug')

    def __str__(self):
        return "{} ({})".format(self.name, self.unit)


class PelotonWorkoutMetrics(PelotonObject):
    """ An object that describes all of the metrics of a given workout
    """

    def __init__(self, **kwargs):
        """ Take a metrics set and objectify it
        """

        self.workout_duration = kwargs.get('duration')
        self.fitness_discipline = kwargs.get('segment_list')[0]['metrics_type']

        # Build summary attributes
        metric_summaries = ['total_output', 'distance', 'calories']
        for metric in kwargs.get('summaries'):
            if metric['slug'] not in metric_summaries:
                get_logger().warning("Unknown metric summary {} found".format(metric['slug']))
                continue

            attr_name = metric['slug'] + '_summary'
            if metric['slug'] == "total_output":
                attr_name = "output_summary"

            setattr(self, attr_name, PelotonMetricSummary(**metric))


        # Build metric details
        metric_categories = ['output', 'cadence', 'resistance', 'speed', 'heart_rate']
        for metric in kwargs.get('metrics'):

            if metric['slug'] not in metric_categories:
                get_logger().warning("Unknown metric category {} found".format(metric['slug']))
                continue

            setattr(self, metric['slug'], PelotonMetric(**metric))

    def __str__(self):
        return self.fitness_discipline


class PelotonInstructor(PelotonObject):
    """ A read-only class that outlines instructor details

    This class should never be invoked directly"""

    def __init__(self):
        raise NotImplementedError()


class PelotonWorkoutSegment(PelotonObject):
    """ A read-only class that outlines instructor details

        This class should never be invoked directly"""

    def __init__(self):
        raise NotImplementedError()


class PelotonWorkoutFactory(PelotonAPI):
    """ Class that handles fetching data and instantiating objects

    See PelotonWorkout for details
    """

    @classmethod
    def list(cls, results_per_page=10):
        """ Return a list of PelotonWorkout instances that describe each workout
        """

        # We need a user ID to list all workouts. @pelotoncycle, please don't this :(
        if cls.user_id is None:
            cls._create_api_session()

        uri = '/api/user/{}/workouts'.format(cls.user_id)
        params = {
            'page': 0,
            'limit': results_per_page,
            'joins': 'ride'
        }

        # Get our first page, which includes number of successive pages
        res = cls._api_request(uri, params).json()

        # Add this pages data to our return list
        ret = [PelotonWorkout(**workout) for workout in res['data']]

        # We've got page 0, so start with page 1
        for i in range(1, res['page_count']):

            params['page'] += 1
            res = cls._api_request(uri, params).json()
            [ret.append(PelotonWorkout(**workout)) for workout in res['data']]

        return ret

    @classmethod
    def get(cls, workout_id):
        """ Get workout details by workout_id
        """

        uri = '/api/workout/{}'.format(workout_id)
        workout = PelotonAPI._api_request(uri).json()
        return PelotonWorkout(**workout)


class PelotonWorkoutMetricsFactory(PelotonAPI):
    """ Class to handle fetching and transformation of metric data
    """

    @classmethod
    def get(cls, workout_id):
        """ Returns a list of PelotonMetric instances for each metric type
        """

        uri = '/api/workout/{}/performance_graph'.format(workout_id)
        params = {
            'every_n': 1
        }

        res = cls._api_request(uri, params).json()
        return PelotonWorkoutMetrics(**res)

