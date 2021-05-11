import json
import logging
import random
import re
import string
import uuid
import warnings
from collections import defaultdict
from datetime import datetime, timedelta

import uszipcode
from flask_babel import lazy_gettext as _
from flask_bcrypt import check_password_hash, generate_password_hash
from geoalchemy2 import Geography, Geometry
from psycopg2.extensions import adapt as sqlescape
from sqlalchemy import (Boolean, Column, DateTime, Enum, ForeignKey, Index,
                        Integer, String, Table, Unicode, UniqueConstraint,
                        create_engine)
from sqlalchemy import exc as sa_exc
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import (aliased, backref, relationship, sessionmaker,
                            validates)
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.orm.session import Session
from sqlalchemy.sql import compiler
from sqlalchemy.sql.expression import (and_, cast, or_, select)

from config import Configuration
from emailer import Emailer
from util import GeometryUtility
from util.language import LanguageCodes
from util.short_client_token import ShortClientTokenTool
from util.string_helpers import random_string

DEBUG = False
Base = declarative_base()


def production_session():
    url = Configuration.database_url()
    logging.debug("Database url: %s", url)
    _db = SessionManager.session(url)

    # The first thing to do after getting a database connection is to
    # set up the logging configuration.
    #
    # If called during a unit test, this will configure logging
    # incorrectly, but 1) this method isn't normally called during
    # unit tests, and 2) package_setup() will call initialize() again
    # with the right arguments.
    from log import LogConfiguration
    LogConfiguration.initialize(_db)
    return _db


def generate_secret():
    """Generate a random secret."""
    return random_string(24)


def get_one(db, model, on_multiple='error', **kwargs):
    q = db.query(model).filter_by(**kwargs)
    try:
        return q.one()
    except MultipleResultsFound as e:
        if on_multiple == 'error':
            raise e
        elif on_multiple == 'interchangeable':
            # These records are interchangeable so we can use whichever one we want.
            # May be a sign of a problem elsewhere. A db-level constraint might be useful.
            q = q.limit(1)
            return q.one()
    except NoResultFound:
        return None


def dump_query(query):
    dialect = query.session.bind.dialect
    statement = query.statement
    comp = compiler.SQLCompiler(dialect, statement)
    comp.compile()
    enc = dialect.encoding
    params = {}
    for (k, v) in comp.params.items():
        if isinstance(v, str):
            v = v.encode(enc)
        params[k] = sqlescape(v)

    return (comp.string.encode(enc) % params).decode(enc)


def get_one_or_create(db, model, create_method='', create_method_kwargs=None, **kwargs):
    one = get_one(db, model, **kwargs)
    if one:
        return (one, False)
    else:
        __transaction = db.begin_nested()
        try:
            if 'on_multiple' in kwargs:
                # This kwarg is supported by get_one() but not by create().
                del kwargs['on_multiple']
            (obj, is_new) = create(db, model, create_method, create_method_kwargs, **kwargs)
            __transaction.commit()
            return (obj, is_new)
        except IntegrityError as e:
            logging.info("INTEGRITY ERROR on %r %r, %r: %r", model, create_method_kwargs, kwargs, e)
            __transaction.rollback()
            return (db.query(model).filter_by(**kwargs).one(), False)


def create(db, model, create_method='', create_method_kwargs=None, **kwargs):
    kwargs.update(create_method_kwargs or {})
    created = getattr(model, create_method, model)(**kwargs)
    db.add(created)
    db.flush()
    return (created, True)


class SessionManager:

    engine_for_url = {}

    @classmethod
    def engine(cls, url=None):
        url = url or Configuration.database_url()
        return create_engine(url, echo=DEBUG)

    @classmethod
    def sessionmaker(cls, url=None):
        engine = cls.engine(url)
        return sessionmaker(bind=engine)

    @classmethod
    def initialize(cls, url):
        if url in cls.engine_for_url:
            engine = cls.engine_for_url[url]
            return engine, engine.connect()

        engine = cls.engine(url)

        Base.metadata.create_all(engine)

        cls.engine_for_url[url] = engine
        return engine, engine.connect()

    @classmethod
    def session(cls, url):
        engine = connection = 0
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=sa_exc.SAWarning)
            engine, connection = cls.initialize(url)
        session = Session(connection)
        cls.initialize_data(session)
        session.commit()
        return session

    @classmethod
    def initialize_data(cls, session):
        pass


class Library(Base):
    """
    A Library typically represents an OPDS server.

    Notes:
        * Libraries generally serve everyone in a specific list of Places.

        * Libraries may also focus on a subset of the places they serve, and may restrict their
          service to certain audiences.

        * Regarding the library_stage and registry_stage fields:
            * Which stage the Library is actually in depends on the combination of
              Library.library_stage (the source institution's opinion) and Library.registry_stage
              (the registry's opinion).
            * If either value is CANCELLED_STAGE, the Library is in CANCELLED_STAGE.
            * Otherwise, if either value is TESTING_STAGE, the Library is in TESTING_STAGE.
            * Otherwise, the Library is in PRODUCTION_STAGE.

        * The PLS (Public Library Surveys) ID comes from the IMLS' annual survey (it isn't
          generated by our database). It enables us to gather data for metrics such as number of
          covered branches and size of service population.

    Library attributes/columns:

        id                      - Integer primary key.

        timestamp               - When our record of this Library was last updated.

        name                    - The official name of the Library. This is not unique because there are many
                                  "Springfield Public Library"s. This is nullable because there's a period during
                                  initial registration where a Library has no name.

        description             - Human-readable explanation of who the Library serves.

        internal_urn            - An internally generated unique URN. This is used in controller URLs to identify
                                  a Library. A registry will always use the same URN to identify a given Library,
                                  even if the Library's OPDS server changes.

        authentication_url      - The URL to the Library's Authentication for OPDS document. This URL may change
                                  over time as libraries move to different servers. This URL is generally unique,
                                  but that's not a database requirement, since a single Library could potentially
                                  have two registry entries.

        opds_url                - The URL to the Library's OPDS server root.

        web_url                 - The URL to the Library's patron-facing web page.

        logo                    - The Library's logo, as a data: URI.

        library_stage           - The source institution's opinion about which stage the Library should be in.

        registry_stage          - The registry's opinion about which stage the Library should be in.

        anonymous_access        - Whether people get books from this Library without authenticating. We store this
                                  specially because it might be useful to filter for libraries of this type.

        online_registration     - Whether eligible people get credentials for this Library through an online
                                  registration process. We store this specially because it might be useful to
                                  filter for libraries of this type.

        short_name              - To issue Short Client Tokens for this Library, the registry must share a
                                  short name and a secret with them.

        shared_secret           - The shared secret is also used to authenticate requests in the case where a
                                  Library's URL has changed.

    Library model relationships:

        aliases                         - Alternate names, e.g. "BPL" for the Brooklyn Public Library

        service_areas                   - Places the Library serves

        audiences                       - Specific Audiences the Library serves

        collections                     - The registry may have information about the library's collections
                                          of materials. The registry doesn't need to know details, but it's
                                          useful to know approximate counts when finding libraries that serve
                                          specific language communities.

        delegated_patron_identifiers    - The registry may keep delegated patron identifiers (basically, Adobe
                                          IDs) for a library's patrons. This allows the library's patrons to
                                          decrypt Adobe ACS-encrypted books without having to license separate
                                          Adobe Vendor ID and without the registry knowing anything about the patrons.


        hyperlinks                      - A Library may have miscellaneous URIs associated with it. Generally
                                          speaking, the registry is only concerned about these URIs insofar as
                                          it needs to verify that they work.
    """
    ##### Class Constants ####################################################  # noqa: E266
    TESTING_STAGE       = 'testing'     # Library should show up in test feed           # noqa: E221
    PRODUCTION_STAGE    = 'production'  # Library should show up in production feed     # noqa: E221
    CANCELLED_STAGE     = 'cancelled'   # Library should not show up in any feed        # noqa: E221
    PLS_ID              = "pls_id"      # Public Library Surveys ID                     # noqa: E221
    US_ZIP_REGEX        = re.compile("^[0-9]{5}$")                                      # noqa: E221
    US_ZIP4_REGEX       = re.compile("^[0-9]{5}-[0-9]{4}$")                             # noqa: E221
    WHITESPACE_REGEX    = re.compile(r"\s+")                                            # noqa: E221

    ##### Public Interface / Magic Methods ###################################  # noqa: E266
    def set_hyperlink(self, rel, *hrefs):
        """
        Make sure Library has a Hyperlink with the given `rel` that points to a Resource with
        one of the given `href`s.

        If there's already a matching Hyperlink, it will be returned unmodified. Otherwise, the
        first item in `hrefs` will be used as the basis for a new Hyperlink, or an existing
        Hyperlink will be modified to use the first item in `hrefs` as its Resource.

        :return: A 2-tuple (Hyperlink, is_modified). `is_modified`
            is True if a new Hyperlink was created _or_ an existing
            Hyperlink was modified.
        """
        if not rel:
            raise ValueError("No link relation was specified")

        if not hrefs:
            raise ValueError("No Hyperlink hrefs were specified")

        default_href = hrefs[0]
        _db = Session.object_session(self)
        (hyperlink, is_modified) = get_one_or_create(_db, Hyperlink, library=self, rel=rel,)

        if hyperlink.href not in hrefs:
            hyperlink.href = default_href
            is_modified = True

        return hyperlink, is_modified

    ##### SQLAlchemy Table properties ########################################  # noqa: E266
    __tablename__ = 'libraries'

    ##### SQLAlchemy non-Column components ###################################  # noqa: E266
    stage_enum = Enum(TESTING_STAGE, PRODUCTION_STAGE, CANCELLED_STAGE, name='library_stage')

    ##### SQLAlchemy Columns #################################################  # noqa: E266
    id = Column(Integer, primary_key=True)
    name = Column(Unicode, index=True)
    description = Column(Unicode)
    internal_urn = Column(Unicode, nullable=False, index=True, unique=True,
                          default=lambda: "urn:uuid:" + str(uuid.uuid4()))
    authentication_url = Column(Unicode, index=True)
    opds_url = Column(Unicode)
    web_url = Column(Unicode)
    timestamp = Column(DateTime, index=True, default=datetime.utcnow, onupdate=datetime.utcnow)
    logo = Column(Unicode)
    _library_stage = Column(stage_enum, index=True, nullable=False, default=TESTING_STAGE, name="library_stage")
    registry_stage = Column(stage_enum, index=True, nullable=False, default=TESTING_STAGE)
    anonymous_access = Column(Boolean, default=False)
    online_registration = Column(Boolean, default=False)
    short_name = Column(Unicode, index=True, unique=True)
    shared_secret = Column(Unicode)

    ##### SQLAlchemy Relationships ###########################################  # noqa: E266
    aliases = relationship("LibraryAlias", backref='library')
    service_areas = relationship('ServiceArea', backref='library')
    audiences = relationship('Audience', secondary='libraries_audiences', back_populates="libraries")
    collections = relationship("CollectionSummary", backref='library')
    delegated_patron_identifiers = relationship("DelegatedPatronIdentifier", backref='library')
    hyperlinks = relationship("Hyperlink", backref='library')

    ##### SQLAlchemy Field Validation ########################################  # noqa: E266
    @validates('short_name')
    def validate_short_name(self, key, value):
        if not value:
            return value
        if '|' in value:
            raise ValueError(
                'Short name cannot contain the pipe character.'
            )
        return value.upper()

    ##### Properties and Getters/Setters #####################################  # noqa: E266
    @property
    def pls_id(self):
        return ConfigurationSetting.for_library(Library.PLS_ID, self)

    @hybrid_property
    def library_stage(self):
        return self._library_stage

    @library_stage.setter
    def library_stage(self, value):
        """A library can't unilaterally go from being in production to not being in production"""
        if self.in_production and value != self.PRODUCTION_STAGE:
            msg = "This library is already in production; only the registry can take it out of production."
            raise ValueError(msg)

        self._library_stage = value

    @property
    def number_of_patrons(self):
        db = Session.object_session(self)

        if not self.in_production:
            return 0  # Count is only meaningful if the library is in production

        query = db.query(DelegatedPatronIdentifier).filter(
            DelegatedPatronIdentifier.type == DelegatedPatronIdentifier.ADOBE_ACCOUNT_ID,
            DelegatedPatronIdentifier.library_id == self.id
        )

        return query.count()

    @property
    def in_production(self):
        """Is this library in production? If library and registry agree on production, it is."""
        return bool(self.library_stage == self.PRODUCTION_STAGE and self.registry_stage == self.PRODUCTION_STAGE)

    @property
    def service_area_name(self):
        """
        Describe Library's service area in a short string a human would understand. Ex: "Kern County, CA"

        This library does the best it can to express a library's service
        area as the name of a single place, but it's not always possible
        since libraries can have multiple service areas.

        TODO: We'll want to fetch a library's ServiceAreas (and their
        Places) as part of the query that fetches libraries, so that
        this doesn't result in extra DB queries per library.

        :return: A string, or None if the library's service area can't be described as a short string.
        """
        by_type = defaultdict(set)

        for a in self.service_areas:   # Group the ServiceAreas by type.
            if not a.place or a.place.type == Place.EVERYWHERE:
                continue

            by_type[a.type].add(a)

        # If there's a single focus area, use it.
        # Otherwise, if there is a single eligibility area, use that.
        service_area = None
        for area_type in ServiceArea.FOCUS, ServiceArea.ELIGIBILITY:
            if len(by_type[area_type]) == 1:
                [service_area] = by_type[area_type]
                break

        if service_area:
            return service_area.place.human_friendly_name

        return None     # No single ServiceArea stands out; can't describe it with a short string

    ##### Class Methods ######################################################  # noqa: E266
    @classmethod
    def for_short_name(cls, _db, short_name):
        """Look up a library by short name."""
        return get_one(_db, Library, short_name=short_name)

    @classmethod
    def for_urn(cls, _db, urn):
        """Look up a library by URN."""
        return get_one(_db, Library, internal_urn=urn)

    @classmethod
    def as_postal_code(cls, query):
        """Try to interpret a query as a postal code."""
        if cls.US_ZIP_REGEX.match(query):
            return query
        elif cls.US_ZIP4_REGEX.match(query):
            return query[:5]
        else:
            return None

    @classmethod
    def random_short_name(cls, duplicate_check=None, max_attempts=20):
        """Generate a random short name for a library.

        Library short names are six uppercase letters.

        :param duplicate_check: Call this function to check whether a
            generated name is a duplicate.
        :param max_attempts: Stop trying to generate a name after this
            many failures.
        """
        attempts = 0
        choice = None
        while choice is None and attempts < max_attempts:
            choice = "".join([random.choice(string.ascii_uppercase) for i in range(6)])

            if duplicate_check and duplicate_check(choice):
                choice = None

            attempts += 1

        if choice is None:  # Something's wrong, need to raise an exception.
            raise ValueError(f"Could not generate random short name after {attempts} attempts!")

        return choice

    @classmethod
    def nearby(cls, _db, target, max_radius=150, production=True):
        """Find libraries whose service areas include or are close to the
        given point.

        :param target: The starting point. May be a Geometry object or
         a 2-tuple (latitude, longitude).
        :param max_radius: How far out from the starting point to search
            for a library's service area, in kilometers.
        :param production: If True, only libraries that are ready for
            production are shown.

        :return: A database query that returns lists of 2-tuples
        (library, distance from starting point). Distances are
        measured in meters.
        """
        # We start with a single point on the globe. Call this Point
        # A.
        if isinstance(target, tuple):
            target = GeometryUtility.point(*target)
        target_geography = cast(target, Geography)

        # Find another point on the globe that's 150 kilometers
        # northeast of Point A. Call this Point B.
        other_point = func.ST_Project(
            target_geography, max_radius*1000, func.radians(90.0)
        )
        other_point = cast(other_point, Geometry)

        # Determine the distance between Point A and Point B, in
        # radians. (150 kilometers is a different number of radians in
        # different parts of the world.)
        distance_to_other_point = func.ST_Distance(target, other_point)

        # Find all Places that are no further away from A than that
        # number of radians.
        nearby = func.ST_DWithin(target,
                                 Place.geometry,
                                 distance_to_other_point)

        # For each library served by such a place, calculate the
        # minimum distance between the library's service area and
        # Point A in meters.
        min_distance = func.min(func.ST_DistanceSphere(target, Place.geometry))

        qu = _db.query(Library).join(Library.service_areas).join(
            ServiceArea.place)
        qu = qu.filter(cls._feed_restriction(production))
        qu = qu.filter(nearby)
        qu = qu.add_columns(
                min_distance).group_by(Library.id).order_by(
                min_distance.asc())
        return qu

    @classmethod
    def search(cls, _db, target, query, production=True):
        """Try as hard as possible to find a small number of libraries
        that match the given query.

        :param target: Order libraries by their distance from this
         point. May be a Geometry object or a 2-tuple (latitude,
         longitude).

        :param query: String to search for.

        :param production: If True, only libraries that are ready for
            production are shown.
        """
        # We don't anticipate a lot of libraries or a lot of
        # localities with the same name, but we need to have _some_
        # kind of limit just to place an upper bound on how bad things
        # can get. This will guarantee we never return more than 20
        # results.
        max_libraries = 10

        if not query:
            return []   # No query, no results.
        if target:
            if isinstance(target, tuple):
                here = GeometryUtility.point(*target)
            else:
                here = target
        else:
            here = None

        library_query, place_query, place_type = cls.query_parts(query)
        # We start with libraries that match the name query.
        if library_query:
            libraries_for_name = cls.search_by_library_name(
                _db, library_query, here, production
            ).limit(max_libraries).all()
        else:
            libraries_for_name = []

        # We tack on any additional libraries that match a place query.
        if place_query:
            libraries_for_location = cls.search_by_location_name(
                _db, place_query, place_type, here, production
            ).limit(max_libraries).all()
        else:
            libraries_for_location = []

        if libraries_for_name and libraries_for_location:
            # Filter out any libraries that show up in both lists.
            for_name = set(libraries_for_name)
            libraries_for_location = [x for x in libraries_for_location if x not in for_name]

        # A lot of libraries list their locations only within their description, so it's worth
        # checking the description for the search term.
        libraries_for_description = cls.search_within_description(
            _db, query, here, production
        ).limit(max_libraries).all()

        return libraries_for_name + libraries_for_location + libraries_for_description

    @classmethod
    def search_by_library_name(cls, _db, name, here=None, production=True):
        """Find libraries whose name or alias matches the given name.

        :param name: Name of the library to search for.
        :param here: Order results by proximity to this location.
        :param production: If True, only libraries that are ready for
            production are shown.
        """
        name_matches = cls.fuzzy_match(Library.name, name)
        alias_matches = cls.fuzzy_match(LibraryAlias.name, name)
        partial_matches = cls.partial_match(Library.name, name)
        return cls.create_query(_db, here, production, name_matches, alias_matches, partial_matches)

    @classmethod
    def search_by_location_name(cls, _db, query, type=None, here=None, production=True):
        """
        Find libraries whose service area overlaps a place with the given name.

        :param query: Name of the place to search for.
        :param type: Restrict results to places of this type.
        :param here: Order results by proximity to this location.
        :param production: If True, only libraries that are ready for
            production are shown.
        """
        # For a library to match, the Place named by the query must
        # intersect a Place served by that library.
        named_place = aliased(Place)
        qu = _db.query(Library).join(
            Library.service_areas).join(
                ServiceArea.place).join(
                    named_place,
                    func.ST_Intersects(Place.geometry, named_place.geometry)
                ).outerjoin(named_place.aliases)

        qu = qu.filter(cls._feed_restriction(production))
        name_match = cls.fuzzy_match(named_place.external_name, query)
        alias_match = cls.fuzzy_match(PlaceAlias.name, query)
        qu = qu.filter(or_(name_match, alias_match))

        if type:
            qu = qu.filter(named_place.type == type)

        if here:
            min_distance = func.min(func.ST_DistanceSphere(here, named_place.geometry))
            qu = qu.add_columns(min_distance)
            qu = qu.group_by(Library.id)
            qu = qu.order_by(min_distance.asc())

        return qu

    @classmethod
    def create_query(cls, _db, here=None, production=True, *args):
        qu = _db.query(Library).outerjoin(Library.aliases)
        if here:
            qu = qu.outerjoin(Library.service_areas).outerjoin(ServiceArea.place)
        qu = qu.filter(or_(*args))
        qu = qu.filter(cls._feed_restriction(production))
        if here:
            # Order by the minimum distance between one of the
            # library's service areas and the current location.
            min_distance = func.min(func.ST_DistanceSphere(here, Place.geometry))
            qu = qu.add_columns(min_distance)
            qu = qu.group_by(Library.id)
            qu = qu.order_by(min_distance.asc())
        return qu

    @classmethod
    def search_within_description(cls, _db, query, here=None, production=True):
        """Find libraries whose descriptions include the search term.

        :param query: The string to search for.
        :param here: Order results by proximity to this location.
        :param production: If True, only libraries that are ready for
            production are shown.
        """
        description_matches = cls.fuzzy_match(Library.description, query)
        partial_matches = cls.partial_match(Library.description, query)
        return cls.create_query(_db, here, production, description_matches, partial_matches)

    @classmethod
    def query_cleanup(cls, query):
        """Clean up a query."""
        query = query.lower()
        query = cls.WHITESPACE_REGEX.sub(" ", query).strip()
        query = query.replace("libary", "library")  # Correct the most common misspelling of 'library'
        return query

    @classmethod
    def query_parts(cls, query):
        """Turn a query received by a user into a set of things to
        check against different bits of the database.
        """
        query = cls.query_cleanup(query)

        postal_code = cls.as_postal_code(query)
        if postal_code:
            # The query is a postal code. Don't even bother searching
            # for a library name -- just find that code.
            return None, postal_code, Place.POSTAL_CODE

        # In theory, absolutely anything could be a library name or
        # alias. We'll let Levenshtein distance take care of minor
        # typos, but we don't process the query very much before
        # seeing if it matches a library name.
        library_query = query

        # If the query looks like a library name, extract a location
        # from it. This will find the public library in Irvine from
        # "irvine public library", even though there is no library
        # called the "Irvine Public Library".
        #
        # NOTE: This will fall down if there is a place with "Library"
        # in the name, but there are no such places in the US.
        place_query = query
        place_type = None
        for indicator in 'public library', 'library':
            if indicator in place_query:
                place_query = place_query.replace(indicator, '').strip()

        place_query, place_type = Place.parse_name(place_query)

        return library_query, place_query, place_type

    @classmethod
    def fuzzy_match(cls, field, value):
        """Create a SQL clause that attempts a fuzzy match of the given
        field against the given value.

        If the field's value is less than six characters, we require
        an exact (case-insensitive) match. Otherwise, we require a
        Levenshtein distance of less than two between the field value and
        the provided value.
        """
        is_long = func.length(field) >= 6
        close_enough = func.levenshtein(func.lower(field), value) <= 2
        long_value_is_approximate_match = (is_long & close_enough)
        exact_match = field.ilike(value)
        return or_(long_value_is_approximate_match, exact_match)

    @classmethod
    def partial_match(cls, field, value):
        """Create a SQL clause that attempts to match a partial value--e.g.
        just one word of a library's name--against the given field."""
        return field.ilike("%{}%".format(value))

    @classmethod
    def get_hyperlink(cls, library, rel):
        link = [x for x in library.hyperlinks if x.rel == rel]
        if len(link) > 0:
            return link[0]

    ##### Private Class Methods ##############################################  # noqa: E266
    @classmethod
    def _feed_restriction(cls, production, library_field=None, registry_field=None):
        """
        Create a SQLAlchemy restriction that only finds libraries that ought to be in the given feed.

        :param production: A boolean. If True, then only libraries in
        the production stage should be included. If False, then
        libraries in the production or testing stages should be
        included.

        :return: A SQLAlchemy expression.
        """
        if library_field is None:
            library_field = Library.library_stage    # The library's opinion

        if registry_field is None:
            registry_field = Library.registry_stage  # The registry's opinion

        prod = cls.PRODUCTION_STAGE
        test = cls.TESTING_STAGE

        if production:      # Both parties must agree that this library is production-ready
            return and_(library_field == prod, registry_field == prod)
        else:               # Both must agree library is in _either_ prod stage or test stage
            return and_(library_field.in_((prod, test)), registry_field.in_((prod, test)))


class LibraryAlias(Base):

    """An alternate name for a library."""
    __tablename__ = 'libraryalias'

    id = Column(Integer, primary_key=True)
    library_id = Column(Integer, ForeignKey('libraries.id'), index=True)
    name = Column(Unicode, index=True)
    language = Column(Unicode(3), index=True)

    __table_args__ = (
        UniqueConstraint('library_id', 'name', 'language'),
    )


class ServiceArea(Base):
    """Designates a geographic area served by a Library.

    A ServiceArea maps a Library to a Place. People living in this
    Place have service from the Library.
    """
    __tablename__ = 'serviceareas'

    id = Column(Integer, primary_key=True)
    library_id = Column(
        Integer, ForeignKey('libraries.id'), index=True
    )

    place_id = Column(
        Integer, ForeignKey('places.id'), index=True
    )

    # A library may have a ServiceArea because people in that area are
    # eligible for service, or because the library specifically
    # focuses on that area.
    ELIGIBILITY = 'eligibility'
    FOCUS = 'focus'
    servicearea_type_enum = Enum(
        ELIGIBILITY, FOCUS, name='servicearea_type'
    )
    type = Column(servicearea_type_enum,
                  index=True, nullable=False, default=ELIGIBILITY)

    __table_args__ = (
        UniqueConstraint('library_id', 'place_id', 'type'),
    )


class Place(Base):
    __tablename__ = 'places'

    # These are the kinds of places we keep track of. These are not
    # supposed to be precise terms. Each census-designated place is
    # called a 'city', even if it's not a city in the legal sense.
    # Countries that call their top-level administrative divisions something
    # other than 'states' can still use 'state' as their type.
    NATION = 'nation'
    STATE = 'state'
    COUNTY = 'county'
    CITY = 'city'
    POSTAL_CODE = 'postal_code'
    LIBRARY_SERVICE_AREA = 'library_service_area'
    EVERYWHERE = 'everywhere'

    id = Column(Integer, primary_key=True)

    # The type of place.
    type = Column(Unicode(255), index=True, nullable=False)

    # The unique ID given to this place in the data source it was
    # derived from.
    external_id = Column(Unicode, index=True)

    # The name given to this place by the data source it was
    # derived from.
    external_name = Column(Unicode, index=True)

    # A canonical abbreviated name for this place. Generally used only
    # for nations and states.
    abbreviated_name = Column(Unicode, index=True)

    # The most convenient place that 'contains' this place. For most
    # places the most convenient parent will be a state. For states,
    # the best parent will be a nation. A nation has no parent; neither
    # does 'everywhere'.
    parent_id = Column(
        Integer, ForeignKey('places.id'), index=True
    )

    children = relationship(
        "Place",
        backref=backref("parent", remote_side=[id]),
        lazy="joined"
    )

    # The geography of the place itself. It is stored internally as a
    # geometry, which means we have to cast to Geography when doing
    # calculations.
    geometry = Column(Geometry(srid=4326), nullable=True)

    aliases = relationship("PlaceAlias", backref='place')

    service_areas = relationship("ServiceArea", backref="place")

    @classmethod
    def everywhere(cls, _db):
        """Return a special Place that represents everywhere.

        This place has no .geometry, so attempts to use it in
        geographic comparisons will fail.
        """
        place, is_new = get_one_or_create(
            _db, Place, type=cls.EVERYWHERE,
            create_method_kwargs=dict(external_id="Everywhere",
                                      external_name="Everywhere")
        )
        return place

    @classmethod
    def default_nation(cls, _db):
        """Return the default nation for this library registry.

        If an incoming coverage area doesn't mention a nation, we'll
        assume it's within this nation.

        :return: The default nation, if one can be found. Otherwise, None.
        """
        default_nation = None
        abbreviation = ConfigurationSetting.sitewide(
            _db, Configuration.DEFAULT_NATION_ABBREVIATION
        ).value
        if abbreviation:
            default_nation = get_one(
                _db, Place, type=Place.NATION, abbreviated_name=abbreviation
            )
            if not default_nation:
                logging.error(
                    "Could not look up default nation %s", abbreviation
                )
        return default_nation

    @classmethod
    def larger_place_types(cls, type):
        """Return a list of place types known to be bigger than `type`.

        Places don't form a strict heirarchy. In particular, ZIP codes
        are not 'smaller' than cities. But counties and cities are
        smaller than states, and states are smaller than nations, so
        if you're searching inside a state for a place called "Japan",
        you know that the nation of Japan is not what you're looking
        for.
        """
        larger = [Place.EVERYWHERE]
        if type not in (Place.NATION, Place.EVERYWHERE):
            larger.append(Place.NATION)
        if type in (Place.COUNTY, Place.CITY, Place.POSTAL_CODE):
            larger.append(Place.STATE)
        if type == Place.CITY:
            larger.append(Place.COUNTY)
        return larger

    @classmethod
    def parse_name(cls, place_name):
        """Try to extract a place type from a name.

        :return: A 2-tuple (place_name, place_type)

        e.g. "Kern County" becomes ("Kern", Place.COUNTY)
        "Arizona State" becomes ("Arizona", Place.STATE)
        "Chicago" becaomes ("Chicago", None)
        """
        check = place_name.lower()
        place_type = None
        if check.endswith(' county'):
            place_name = place_name[:-7]
            place_type = Place.COUNTY

        if check.endswith(' state'):
            place_name = place_name[:-6]
            place_type = Place.STATE
        return place_name, place_type

    @classmethod
    def lookup_by_name(cls, _db, name, place_type=None):
        """Look up one or more Places by name.
        """
        if not place_type:
            name, place_type = cls.parse_name(name)
        qu = _db.query(Place).outerjoin(PlaceAlias).filter(
            or_(Place.external_name == name, Place.abbreviated_name == name,
                PlaceAlias.name == name)
        )
        if place_type:
            qu = qu.filter(Place.type == place_type)
        else:
            # The place type "county" is excluded unless it was
            # explicitly asked for (e.g. "Cook County"). This is to
            # avoid ambiguity in the many cases when a state contains
            # a county and a city with the same name. In all realistic
            # cases, someone using "Foo" to talk about a library
            # service area is referring to the city of Foo, not Foo
            # County -- if they want Foo County they can say "Foo
            # County".
            qu = qu.filter(Place.type != Place.COUNTY)
        return qu

    @classmethod
    def lookup_one_by_name(cls, _db, name, place_type=None):
        return cls.lookup_by_name(_db, name, place_type).one()

    @classmethod
    def to_geojson(cls, _db, *places):
        """Convert one or more Place objects to a dictionary that will become
        a GeoJSON document when converted to JSON.
        """
        geojson = select(
            [func.ST_AsGeoJSON(Place.geometry)]
        ).where(
            Place.id.in_([x.id for x in places])
        )
        results = [x[0] for x in _db.execute(geojson)]
        if len(results) == 1:
            # There's only one item, and it is a valid
            # GeoJSON document on its own.
            return json.loads(results[0])

        # We have either more or less than one valid item.
        # In either case, a GeometryCollection is appropriate.
        body = {"type": "GeometryCollection", "geometries": [json.loads(x) for x in results]}
        return body

    @classmethod
    def name_parts(cls, name):
        """Split a nested geographic name into parts.

        "Boston, MA" is split into ["MA", "Boston"]
        "Lake County, Ohio, USA" is split into
        ["USA", "Ohio", "Lake County"]

        There is no guarantee that these place names correspond to
        Places in the database.

        :param name: The name to split into parts.
        :return: A list of place names, with the largest place at the front
           of the list.
        """
        return [x.strip() for x in reversed(name.split(",")) if x.strip()]

    @property
    def human_friendly_name(self):
        """Generate the sort of string a human would recognize as an
        unambiguous name for this place.

        This is in some sense the opposite of parse_name.

        :return: A string, or None if there is no human-friendly name for
           this place.
        """
        if self.type == self.EVERYWHERE:
            # 'everywhere' is not a distinct place with a well-known name.
            return None
        if self.parent and self.parent.type == self.STATE:
            parent = self.parent.abbreviated_name or self.parent.external_name
            if self.type == Place.COUNTY:
                # Renfrew County, ON
                return "{} County, {}".format(self.external_name, parent)
            elif self.type == Place.CITY:
                # Montgomery, AL
                return "{}, {}".format(self.external_name, parent)

        # All other cases:
        #  93203
        #  Texas
        #  France
        return self.external_name

    def overlaps_not_counting_border(self, qu):
        """Modifies a filter to find places that have points inside this
        Place, not counting the border.

        Connecticut has no points inside New York, but the two states
        share a border. This method creates a more real-world notion
        of 'inside' that does not count a shared border.
        """
        intersects = Place.geometry.intersects(self.geometry)
        touches = func.ST_Touches(Place.geometry, self.geometry)
        return qu.filter(intersects).filter(touches == False)  # noqa: E712

    def lookup_inside(self, name, using_overlap=False, using_external_source=True):

        """Look up a named Place that is geographically 'inside' this Place.

        :param name: The name of a place, such as "Boston" or
        "Calabasas, CA", or "Cook County".

        :param using_overlap: If this is true, then place A is
        'inside' place B if their shapes overlap, not counting
        borders. For example, Montgomery is 'inside' Montgomery
        County, Alabama, and the United States. However, Alabama is
        not 'inside' Georgia (even though they share a border).

        If `using_overlap` is false, then place A is 'inside' place B
        only if B is the .parent of A. In this case, Alabama is
        considered to be 'inside' the United States, but Montgomery is
        not -- the only place it's 'inside' is Alabama. Checking this way
        is much faster, so it's the default.

        :param using_external_source: If this is True, then if no named
        place can be found in the database, the uszipcodes library
        will be used in an attempt to find some equivalent postal codes.

        :return: A Place object, or None if no match could be found.

        :raise MultipleResultsFound: If more than one Place with the
        given name is 'inside' this Place.

        """
        parts = Place.name_parts(name)
        if len(parts) > 1:
            # We're trying to look up a scoped name such as "Boston,
            # MA". `name_parts` has turned "Boston, MA" into ["MA",
            # "Boston"].
            #
            # Now we need to look for "MA" inside ourselves, and then
            # look for "Boston" inside the object we get back.
            look_in_here = self
            for part in parts:
                look_in_here = look_in_here.lookup_inside(part, using_overlap)
                if not look_in_here:
                    # A link in the chain has failed. Return None
                    # immediately.
                    return None
            # Every link in the chain has succeeded, and `must_be_inside`
            # now contains the Place we were looking for.
            return look_in_here

        # If we get here, it means we're looking up "Boston" within
        # Massachussets, or "Kern County" within the United States.
        # In other words, we expect to find at most one place with
        # this name inside the `must_be_inside` object.
        #
        # If we find more than one, it's an error. The name should
        # have been scoped better. This will happen if you search for
        # "Springfield" or "Lake County" within the United States,
        # instead of specifying which state you're talking about.
        _db = Session.object_session(self)
        qu = Place.lookup_by_name(_db, name).filter(Place.type != self.type)

        # Don't look in a place type known to be 'bigger' than this
        # place.
        exclude_types = Place.larger_place_types(self.type)
        qu = qu.filter(~Place.type.in_(exclude_types))

        if self.type == self.EVERYWHERE:
            # The concept of 'inside' is not relevant because every
            # place is 'inside' EVERYWHERE. We are really trying to
            # find one and only one place with a certain name.
            pass
        else:
            if using_overlap and self.geometry is not None:
                qu = self.overlaps_not_counting_border(qu)
            else:
                parent = aliased(Place)
                grandparent = aliased(Place)
                qu = qu.join(parent, Place.parent_id == parent.id)
                qu = qu.outerjoin(grandparent, parent.parent_id == grandparent.id)

                # For postal codes, but no other types of places, we
                # allow the lookup to skip a level. This lets you look
                # up "93203" within a state *or* within the nation.
                postal_code_grandparent_match = and_(
                    Place.type == Place.POSTAL_CODE, grandparent.id == self.id,
                )
                qu = qu.filter(
                    or_(Place.parent == self, postal_code_grandparent_match)
                )

        places = qu.all()
        if len(places) == 0:
            if using_external_source:
                # We don't have any matching places in the database _now_,
                # but there's a possibility we can find a representative
                # postal code.
                return self.lookup_one_through_external_source(name)
            else:
                # We're not allowed to use uszipcodes, probably
                # because this method was called by
                # lookup_through_external_source.
                return None
        if len(places) > 1:
            raise MultipleResultsFound(
                "More than one place called %s inside %s." % (
                    name, self.external_name
                )
            )
        return places[0]

    def lookup_one_through_external_source(self, name):
        """Use an external source to find a Place that is a) inside `self`
        and b) identifies the place human beings call `name`.

        Currently the only way this might work is when using
        uszipcodes to look up a city inside a state. In this case the result
        will be a Place representing one of the city's postal codes.

        :return: A Place, or None if the lookup fails.
        """
        if self.type != Place.STATE:
            # uszipcodes keeps track of places in terms of their state.
            return None

        search = uszipcode.SearchEngine(simple_zipcode=True)
        state = self.abbreviated_name
        uszipcode_matches = []
        if (state in search.state_to_city_mapper and name in search.state_to_city_mapper[state]):
            # The given name is an exact match for one of the
            # cities. Let's look up every ZIP code for that city.
            uszipcode_matches = search.by_city_and_state(
                name, state, returns=None
            )

        # Look up a Place object for each ZIP code and return the
        # first one we actually know about.
        #
        # Set using_external_source to False to eliminate the
        # possibility of wasted effort or (I don't think this can
        # happen) infinite recursion.
        for match in uszipcode_matches:
            place = self.lookup_inside(
                match.zipcode, using_external_source=False
            )
            if place:
                return place

    def served_by(self):
        """Find all Libraries with a ServiceArea whose Place overlaps
        this Place, not counting the border.

        A Library whose ServiceArea borders this place, but does not
        intersect this place, is not counted. This way, the state
        library from the next state over doesn't count as serving your
        state.
        """
        _db = Session.object_session(self)
        qu = _db.query(Library).join(Library.service_areas).join(
            ServiceArea.place)
        qu = self.overlaps_not_counting_border(qu)
        return qu

    def __repr__(self):
        if self.parent:
            parent = self.parent.external_name
        else:
            parent = None
        if self.abbreviated_name:
            abbr = "abbr=%s " % self.abbreviated_name
        else:
            abbr = ''
        output = "<Place: %s type=%s %sexternal_id=%s parent=%s>" % (
            self.external_name, self.type, abbr, self.external_id, parent
        )
        return str(output)


class PlaceAlias(Base):

    """An alternate name for a place."""
    __tablename__ = 'placealiases'

    id = Column(Integer, primary_key=True)
    place_id = Column(Integer, ForeignKey('places.id'), index=True)
    name = Column(Unicode, index=True)
    language = Column(Unicode(3), index=True)

    __table_args__ = (
        UniqueConstraint('place_id', 'name', 'language'),
    )


class Audience(Base):
    """A class of person served by a library."""
    __tablename__ = 'audiences'

    # The general public
    PUBLIC = "public"

    # Pre-university students
    EDUCATIONAL_PRIMARY = "educational-primary"

    # University students
    EDUCATIONAL_SECONDARY = "educational-secondary"

    # Academics and researchers
    RESEARCH = "research"

    # People with print disabilities
    PRINT_DISABILITY = "print-disability"

    # A catch-all for other specialized audiences.
    OTHER = "other"

    KNOWN_AUDIENCES = [
        PUBLIC, EDUCATIONAL_PRIMARY, EDUCATIONAL_SECONDARY, RESEARCH,
        PRINT_DISABILITY, OTHER
    ]

    id = Column(Integer, primary_key=True)
    name = Column(Unicode, index=True, unique=True)

    libraries = relationship("Library", secondary='libraries_audiences',
                             back_populates="audiences")

    @classmethod
    def lookup(cls, _db, name):
        if name not in cls.KNOWN_AUDIENCES:
            raise ValueError(_("Unknown audience: %(name)s", name=name))
        audience, is_new = get_one_or_create(_db, Audience, name=name)
        return audience


class CollectionSummary(Base):
    """A summary of a collection held by a library.

    We only need to know the language of the collection and
    approximately how big it is.
    """
    __tablename__ = 'collectionsummaries'

    id = Column(Integer, primary_key=True)
    library_id = Column(Integer, ForeignKey('libraries.id'), index=True)
    language = Column(Unicode)
    size = Column(Integer)

    @classmethod
    def set(cls, library, language, size):
        """Create or update a CollectionSummary for the given
        library and language.

        :return: An up-to-date CollectionSummary.
        """
        _db = Session.object_session(library)

        size = int(size)
        if size < 0:
            raise ValueError(_("Collection size cannot be negative."))

        # This might return None, which is fine. We'll store it as a
        # collection with an unknown language. This also covers the
        # case where the library specifies its collection size but
        # doesn't mention any languages.
        language_code = LanguageCodes.string_to_alpha_3(language)

        summary, is_new = get_one_or_create(
            _db, CollectionSummary, library=library,
            language=language_code
        )
        summary.size = size
        return summary


Index("ix_collectionsummary_language_size", CollectionSummary.language, CollectionSummary.size)


class Hyperlink(Base):
    """A link between a Library and a Resource.

    We trust that the Resource is actually associated with the Library
    because the library told us about it; either directly, during
    registration, or by putting a link in its Authentication For OPDS
    document.
    """
    INTEGRATION_CONTACT_REL = "http://librarysimplified.org/rel/integration-contact"
    COPYRIGHT_DESIGNATED_AGENT_REL = "http://librarysimplified.org/rel/designated-agent/copyright"
    HELP_REL = "help"

    # Descriptions of the link relations, used in emails.
    REL_DESCRIPTIONS = {
        INTEGRATION_CONTACT_REL: "integration point of contact",
        COPYRIGHT_DESIGNATED_AGENT_REL: "copyright designated agent",
        HELP_REL: "patron help contact address",
    }

    # Hyperlinks with these relations are not for public consumption.
    PRIVATE_RELS = [INTEGRATION_CONTACT_REL]

    __tablename__ = 'hyperlinks'

    id = Column(Integer, primary_key=True)
    rel = Column(Unicode, index=True, nullable=False)
    library_id = Column(Integer, ForeignKey('libraries.id'), index=True)
    resource_id = Column(Integer, ForeignKey('resources.id'), index=True)

    # A Library can have multiple links with the same rel, but we only
    # need to keep track of one.
    __table_args__ = (
        UniqueConstraint('library_id', 'rel'),
    )

    @hybrid_property
    def href(self):
        if not self.resource:
            return None
        return self.resource.href

    @href.setter
    def href(self, url):
        _db = Session.object_session(self)
        resource, is_new = get_one_or_create(_db, Resource, href=url)
        self.resource = resource

    def notify(self, emailer, url_for):
        """Notify the target of this hyperlink that it is, in fact,
        a target of the hyperlink.

        If the underlying resource needs a new validation, an
        ADDRESS_NEEDS_CONFIRMATION email will be sent, asking the person on
        the other end to confirm the address. Otherwise, an
        ADDRESS_DESIGNATED email will be sent, informing the person on
        the other end that their (probably already validated) email
        address was associated with another library.

        :param emailer: An Emailer, for sending out the email.
        :param url_for: An implementation of Flask's url_for, used to
            generate a validation link if necessary.
        """
        if not emailer or not url_for:
            # We can't actually send any emails.
            return
        _db = Session.object_session(self)

        # These shouldn't happen, but just to be safe, do nothing if
        # this Hyperlink is disconnected from the other data model
        # objects it needs to do its job.
        resource = self.resource
        library = self.library
        if not resource or not library:
            return

        # Default to sending an informative email with no validation
        # link.
        email_type = Emailer.ADDRESS_DESIGNATED
        to_address = resource.href
        if to_address.startswith('mailto:'):
            to_address = to_address[7:]

        # Make sure there's a Validation object associated with this Resource.
        if resource.validation is None:
            resource.validation, is_new = create(_db, Validation)
        else:
            is_new = False
        validation = resource.validation

        if is_new or not validation.active:
            # Either this Validation was just created or it expired
            # before being verified. Restart the validation process
            # and send an email that includes a validation link.
            validation.restart()
            email_type = Emailer.ADDRESS_NEEDS_CONFIRMATION

        # Create values for all the variables expected by the default
        # templates.
        template_args = dict(
            rel_desc=Hyperlink.REL_DESCRIPTIONS.get(self.rel, self.rel),
            library=library.name,
            library_web_url=library.web_url,
            email=to_address,
            registry_support=ConfigurationSetting.sitewide(
                _db, Configuration.REGISTRY_CONTACT_EMAIL
            ).value,
        )
        if email_type == Emailer.ADDRESS_NEEDS_CONFIRMATION:
            template_args['confirmation_link'] = url_for(
                "confirm_resource", resource_id=resource.id, secret=validation.secret
            )
        body = emailer.send(email_type, to_address, **template_args)
        return body


class Resource(Base):
    """A URI, potentially linked to multiple libraries, or to a single
    library through multiple relationships.

    e.g. a library consortium may use a single email address as the
    patron help address and the integration contact address for all of
    its libraries. That address only needs to be validated once.
    """
    __tablename__ = 'resources'

    id = Column(Integer, primary_key=True)
    href = Column(Unicode, nullable=False, index=True, unique=True)
    hyperlinks = relationship("Hyperlink", backref="resource")

    # Every Resource may have at most one Validation. There's no
    # need to validate it separately for every relationship.
    validation_id = Column(Integer, ForeignKey('validations.id'),
                           index=True)

    def restart_validation(self):
        """Start or restart the validation process for this resource."""
        if not self.validation:
            _db = Session.object_session(self)
            self.validation, ignore = create(_db, Validation)
        self.validation.restart()
        return self.validation


class Validation(Base):
    """An attempt (successful, in-progress, or failed) to validate a
    Resource.
    """
    __tablename__ = 'validations'

    EXPIRES_AFTER = timedelta(days=1)

    id = Column(Integer, primary_key=True)
    success = Column(Boolean, index=True, default=False)
    started_at = Column(DateTime, index=True, nullable=False, default=datetime.utcnow)

    # Used in OPDS catalogs to convey the status of a validation attempt.
    STATUS_PROPERTY = "https://schema.org/reservationStatus"

    # These constants are used in OPDS catalogs as values of
    # schema:reservationStatus.
    CONFIRMED = "https://schema.org/ReservationConfirmed"
    IN_PROGRESS = "https://schema.org/ReservationPending"
    INACTIVE = "https://schema.org/ReservationCancelled"

    # The only way to validate a Resource is to prove you know the
    # corresponding secret.
    secret = Column(Unicode, default=generate_secret, unique=True)

    resource = relationship(
        "Resource", backref=backref("validation", uselist=False), uselist=False
    )

    def restart(self):
        """Start a new validation attempt, cancelling any previous attempt.

        This does not send out a validation email -- that needs to be
        handled separately by something capable of generating the URL
        to the validation controller.
        """
        self.started_at = datetime.utcnow()
        self.secret = generate_secret()
        self.success = False

    @property
    def deadline(self):
        if self.success:
            return None
        return self.started_at + self.EXPIRES_AFTER

    @property
    def active(self):
        """Is this Validation still active?

        An inactive Validation can't be marked as successful -- it
        needs to be reset.
        """
        now = datetime.utcnow()
        return not self.success and now < self.deadline

    def mark_as_successful(self):
        """Register the fact that the validation attempt has succeeded."""
        if self.success:
            raise Exception("This validation has already succeeded.")
        if not self.active:
            raise Exception("This validation has expired.")
        self.secret = None
        self.success = True

        # TODO: This may cause one or more libraries to switch from
        # "not completely validated" to "completely validated".


class DelegatedPatronIdentifier(Base):
    """An identifier generated by the library registry which identifies a
    patron of one of the libraries.

    This is probably an Adobe Account ID.
    """
    ADOBE_ACCOUNT_ID = 'Adobe Account ID'

    __tablename__ = 'delegatedpatronidentifiers'
    id = Column(Integer, primary_key=True)
    type = Column(String(255), index=True)
    library_id = Column(Integer, ForeignKey('libraries.id'), index=True)

    # This is the ID the foreign library gives us when referring to
    # this patron.
    patron_identifier = Column(String(255), index=True)

    # This is the identifier we made up for the patron. This is what the
    # foreign library is trying to look up.
    delegated_identifier = Column(String)

    __table_args__ = (
        UniqueConstraint('type', 'library_id', 'patron_identifier'),
    )

    @classmethod
    def get_one_or_create(
            cls, _db, library, patron_identifier, identifier_type,
            identifier_or_identifier_factory
    ):
        """Look up the delegated identifier for the given patron. If there is
        none, create one.

        :param library: The Library in charge of the patron's record.

        :param patron_identifier: An identifier used by that library
         to distinguish between this patron and others. This should be
         an identifier created solely for the purpose of identifying
         the patron with the library registry, and not (e.g.) the
         patron's barcode.

        :param identifier_type: The type of the delegated identifier
         to look up. (probably ADOBE_ACCOUNT_ID)

        :param identifier_or_identifier_factory: If this patron does
         not have a DelegatedPatronIdentifier, one will be created,
         and this object will be used to set its
         .delegated_identifier. If a string is passed in,
         .delegated_identifier will be that string. If a function is
         passed in, .delegated_identifier will be set to the return
         value of the function call.

        :return: A 2-tuple (DelegatedPatronIdentifier, is_new)

        """
        identifier, is_new = get_one_or_create(
            _db, DelegatedPatronIdentifier, library=library,
            patron_identifier=patron_identifier, type=identifier_type
        )
        if is_new:
            if callable(identifier_or_identifier_factory):
                # We are in charge of creating the delegated identifier.
                delegated_identifier = identifier_or_identifier_factory()
            else:
                # We haven't heard of this patron before, but some
                # other server does know about them, and they told us
                # this is the delegated identifier.
                delegated_identifier = identifier_or_identifier_factory
            identifier.delegated_identifier = delegated_identifier
        return identifier, is_new


class ShortClientTokenDecoder(ShortClientTokenTool):
    """Turn a short client token into a DelegatedPatronIdentifier.

    Used by the library registry. Not used by the circulation manager.

    See util/short_client_token.py for the corresponding encoder.
    """

    def uuid(self):
        """Create a new UUID URN compatible with the Vendor ID system."""
        u = str(uuid.uuid1(self.node_value))
        # This chop is required by the spec. I have no idea why, but
        # since the first part of the UUID is the least significant,
        # it doesn't do much damage.
        value = "urn:uuid:0" + u[1:]
        return value

    def __init__(self, node_value, delegates):
        super(ShortClientTokenDecoder, self).__init__()
        if isinstance(node_value, str):
            # The node value may be stored in hex form (that's how
            # Adobe gives it out) or as the equivalent decimal number.
            if node_value.startswith('0x'):
                node_value = int(node_value, 16)
            else:
                node_value = int(node_value)
        self.node_value = node_value
        self.delegates = delegates

    def decode(self, _db, token):
        """Decode a short client token.

        :return: a DelegatedPatronIdentifier

        :raise ValueError: When the token is not valid for any reason.
        """
        if not token:
            raise ValueError("Cannot decode an empty token.")
        if '|' not in token:
            raise ValueError(
                'Supposed client token "%s" does not contain a pipe.' % token
            )

        username, password = token.rsplit('|', 1)
        return self.decode_two_part(_db, username, password)

    def decode_two_part(self, _db, username, password):
        """Decode a short client token that has already been split into
        two parts.
        """
        library = patron_identifier = account_id = None

        # No matter how we do this, if we're going to create
        # a DelegatedPatronIdentifier, we need to extract the Library
        # and the library's identifier for this patron from the 'username'
        # part of the token.
        #
        # If this username/password is not actually a Short Client
        # Token, this will raise an exception, which gives us a quick
        # way to bail out.
        library, expires, patron_identifier = self._split_token(
            _db, username
        )

        # First see if a delegate can give us an Adobe ID (account_id)
        # for this patron.
        for delegate in self.delegates:
            try:
                account_id, label, content = delegate.sign_in_standard(
                    username, password
                )
            except Exception:
                # This delegate couldn't help us.
                pass
            if account_id:
                # We got it -- no need to keep checking delegates.
                break

        if not account_id:
            # The delegates couldn't help us; let's try to do it
            # ourselves.
            try:
                signature = self.adobe_base64_decode(password)
            except Exception:
                raise ValueError("Invalid password: %s" % password)

            patron_identifier, account_id = self._decode(
                _db, username, signature
            )

        # If we got this far, we have a Library, a patron_identifier,
        # and an account_id.
        delegated_patron_identifier, is_new = (
            DelegatedPatronIdentifier.get_one_or_create(
                _db, library, patron_identifier,
                DelegatedPatronIdentifier.ADOBE_ACCOUNT_ID, account_id
            )
        )
        return delegated_patron_identifier

    def _split_token(self, _db, token):
        """Split the 'username' part of a Short Client Token.

        :return: A 3-tuple (Library, expiration, foreign patron identifier)
        """
        if token.count('|') < 2:
            raise ValueError("Invalid client token: %s" % token)
        library_short_name, expiration, patron_identifier = token.split("|", 2)
        library_short_name = library_short_name.upper()

        # Look up the Library object based on short name.
        library = get_one(_db, Library, short_name=library_short_name)
        if not library:
            raise ValueError(
                "I don't know how to handle tokens from library \"%s\"" % library_short_name
            )
        try:
            expiration = float(expiration)
        except ValueError:
            raise ValueError('Expiration time "%s" is not numeric.' % expiration)
        return library, expiration, patron_identifier

    def _decode(self, _db, token, supposed_signature):
        """Make sure a client token is properly formatted, correctly signed,
        and not expired.
        """
        library, expiration, patron_identifier = self._split_token(_db, token)
        secret = library.shared_secret

        # We don't police the content of the patron identifier but there
        # has to be _something_ there.
        if not patron_identifier:
            raise ValueError(
                "Token %s has empty patron identifier." % token
            )

        # Don't bother checking an expired token.
        #
        # Currently there are two ways of specifying a token's
        # expiration date: as a number of minutes since self.SCT_EPOCH
        # or as a number of seconds since self.JWT_EPOCH.
        now = datetime.utcnow()

        # NOTE: The JWT code needs to be removed by the year 4869 or
        # this will break.
        if expiration < 1500000000:
            # This is a number of minutes since the start of 2017.
            expiration = self.SCT_EPOCH + timedelta(
                minutes=expiration
            )
        else:
            # This is a number of seconds since the start of 1970.
            expiration = self.JWT_EPOCH + timedelta(seconds=expiration)

        if expiration < now:
            raise ValueError(
                "Token %s expired at %s (now is %s)." % (
                    token, expiration, now
                )
            )

        # Sign the token and check against the provided signature.
        key = self.signer.prepare_key(secret)
        token_bytes = token.encode("utf8")
        actual_signature = self.signer.sign(token_bytes, key)

        if actual_signature != supposed_signature:
            raise ValueError(
                "Invalid signature for %s." % token
            )

        # We have a Library, and a patron identifier which we know is valid.
        # Find or create a DelegatedPatronIdentifier for this person.
        return patron_identifier, self.uuid


class ExternalIntegration(Base):

    """An external integration contains configuration for connecting
    to a third-party API.
    """

    # Possible goals of ExternalIntegrations.

    # These integrations are associated with external services such as
    # Adobe Vendor ID, which manage access to DRM-dependent content.
    DRM_GOAL = 'drm'

    # Integrations with DRM_GOAL
    ADOBE_VENDOR_ID = 'Adobe Vendor ID'

    # These integrations are associated with external services that
    # collect logs of server-side events.
    LOGGING_GOAL = 'logging'

    # Integrations with LOGGING_GOAL
    INTERNAL_LOGGING = 'Internal logging'
    LOGGLY = 'Loggly'

    # These integrations are for sending email.
    EMAIL_GOAL = 'email'

    # Integrations with EMAIL_GOAL
    SMTP = 'SMTP'

    # If there is a special URL to use for access to this API,
    # put it here.
    URL = "url"

    # If access requires authentication, these settings represent the
    # username/password or key/secret combination necessary to
    # authenticate. If there's a secret but no key, it's stored in
    # 'password'.
    USERNAME = "username"
    PASSWORD = "password"

    __tablename__ = 'externalintegrations'
    id = Column(Integer, primary_key=True)

    # Each integration should have a protocol (explaining what type of
    # code or network traffic we need to run to get things done) and a
    # goal (explaining the real-world goal of the integration).
    #
    # Basically, the protocol is the 'how' and the goal is the 'why'.
    protocol = Column(Unicode, nullable=False)
    goal = Column(Unicode, nullable=True)

    # A unique name for this ExternalIntegration. This is primarily
    # used to identify ExternalIntegrations from command-line scripts.
    name = Column(Unicode, nullable=True, unique=True)

    # Any additional configuration information goes into
    # ConfigurationSettings.
    settings = relationship(
        "ConfigurationSetting", backref="external_integration",
        lazy="joined", cascade="save-update, merge, delete, delete-orphan",
    )

    def __repr__(self):
        return "<ExternalIntegration: protocol=%s goal='%s' settings=%d ID=%d>" % (
            self.protocol, self.goal, len(self.settings), self.id)

    @classmethod
    def lookup(cls, _db, protocol, goal):
        integrations = _db.query(cls).filter(
            cls.protocol == protocol, cls.goal == goal
        )

        integrations = integrations.all()
        if len(integrations) > 1:
            logging.warn("Multiple integrations found for '%s'/'%s'" % (protocol, goal))

        if not integrations:
            return None
        return integrations[0]

    @hybrid_property
    def url(self):
        return self.setting(self.URL).value

    @url.setter
    def url(self, new_url):
        self.set_setting(self.URL, new_url)

    @hybrid_property
    def username(self):
        return self.setting(self.USERNAME).value

    @username.setter
    def username(self, new_username):
        self.set_setting(self.USERNAME, new_username)

    @hybrid_property
    def password(self):
        return self.setting(self.PASSWORD).value

    @password.setter
    def password(self, new_password):
        return self.set_setting(self.PASSWORD, new_password)

    def set_setting(self, key, value):
        """Create or update a key-value setting for this ExternalIntegration."""
        setting = self.setting(key)
        setting.value = value
        return setting

    def setting(self, key):
        """Find or create a ConfigurationSetting on this ExternalIntegration.

        :param key: Name of the setting.
        :return: A ConfigurationSetting
        """
        return ConfigurationSetting.for_externalintegration(
            key, self
        )

    def explain(self, include_secrets=False):
        """Create a series of human-readable strings to explain an
        ExternalIntegration's settings.

        :param include_secrets: For security reasons,
           sensitive settings such as passwords are not displayed by default.

        :return: A list of explanatory strings.
        """
        lines = []
        lines.append("ID: %s" % self.id)
        if self.name:
            lines.append("Name: %s" % self.name)
        lines.append("Protocol/Goal: %s/%s" % (self.protocol, self.goal))

        def key(setting):
            if setting.library:
                return setting.key, setting.library.name
            return (setting.key, None)
        for setting in sorted(self.settings, key=key):
            explanation = "%s='%s'" % (setting.key, setting.value)
            if setting.library:
                explanation = "%s (applies only to %s)" % (
                    explanation, setting.library.name
                )
            if include_secrets or not setting.is_secret:
                lines.append(explanation)
        return lines


class ConfigurationSetting(Base):
    """An extra piece of site configuration.

    A ConfigurationSetting may be associated with an
    ExternalIntegration, a Library, both, or neither.

    * The secret used by the circulation manager to sign OAuth bearer
      tokens is not associated with an ExternalIntegration or with a
      Library.

    * The link to a library's privacy policy is associated with the
      Library, but not with any particular ExternalIntegration.

    * The "website ID" for an Overdrive collection is associated with
      an ExternalIntegration (the Overdrive integration), but not with
      any particular Library (since multiple libraries might share an
      Overdrive collection).

    * The "identifier prefix" used to determine which library a patron
      is a patron of, is associated with both a Library and an
      ExternalIntegration.
    """
    __tablename__ = 'configurationsettings'
    id = Column(Integer, primary_key=True)
    external_integration_id = Column(
        Integer, ForeignKey('externalintegrations.id'), index=True
    )
    library_id = Column(
        Integer, ForeignKey('libraries.id'), index=True
    )
    key = Column(Unicode, index=True)
    _value = Column(Unicode, name="value")

    __table_args__ = (
        UniqueConstraint('external_integration_id', 'library_id', 'key'),
    )

    def __repr__(self):
        return '<ConfigurationSetting: key=%s, ID=%d>' % (
            self.key, self.id)

    @classmethod
    def sitewide_secret(cls, _db, key):
        """Find or create a sitewide shared secret.

        The value of this setting doesn't matter, only that it's
        unique across the site and that it's always available.
        """
        secret = ConfigurationSetting.sitewide(_db, key)
        if not secret.value:
            secret.value = generate_secret()
            # Commit to get this in the database ASAP.
            _db.commit()
        return secret.value

    @classmethod
    def explain(cls, _db, include_secrets=False):
        """Explain all site-wide ConfigurationSettings."""
        lines = []
        site_wide_settings = []

        for setting in _db.query(ConfigurationSetting).filter(
                ConfigurationSetting.library_id == None).filter(         # noqa: E711
                    ConfigurationSetting.external_integration == None):  # noqa: E711
            if not include_secrets and setting.key.endswith("_secret"):
                continue
            site_wide_settings.append(setting)
        if site_wide_settings:
            lines.append("Site-wide configuration settings:")
            lines.append("---------------------------------")
        for setting in sorted(site_wide_settings, key=lambda s: s.key):
            lines.append("%s='%s'" % (setting.key, setting.value))
        return lines

    @classmethod
    def sitewide(cls, _db, key):
        """Find or create a sitewide ConfigurationSetting."""
        return cls.for_library_and_externalintegration(_db, key, None, None)

    @classmethod
    def for_library(cls, key, library):
        """Find or create a ConfigurationSetting for the given Library."""
        _db = Session.object_session(library)
        return cls.for_library_and_externalintegration(_db, key, library, None)

    @classmethod
    def for_externalintegration(cls, key, externalintegration):
        """Find or create a ConfigurationSetting for the given
        ExternalIntegration.
        """
        _db = Session.object_session(externalintegration)
        return cls.for_library_and_externalintegration(
            _db, key, None, externalintegration
        )

    @classmethod
    def for_library_and_externalintegration(
            cls, _db, key, library, external_integration
    ):
        """Find or create a ConfigurationSetting associated with a Library
        and an ExternalIntegration.
        """
        library_id = None
        if library:
            library_id = library.id
        setting, ignore = get_one_or_create(
            _db, ConfigurationSetting,
            library_id=library_id, external_integration=external_integration,
            key=key
        )
        return setting

    @property
    def library(self):
        _db = Session.object_session(self)
        if self.library_id:
            return get_one(_db, Library, id=self.library_id)
        return None

    @hybrid_property
    def value(self):
        """What's the current value of this configuration setting?

        If not present, the value may be inherited from some other
        ConfigurationSetting.
        """
        if self._value:
            # An explicitly set value always takes precedence.
            return self._value
        elif self.library_id and self.external_integration:
            # This is a library-specific specialization of an
            # ExternalIntegration. Treat the value set on the
            # ExternalIntegration as a default.
            return self.for_externalintegration(
                self.key, self.external_integration).value
        elif self.library_id:
            # This is a library-specific setting. Treat the site-wide
            # value as a default.
            _db = Session.object_session(self)
            return self.sitewide(_db, self.key).value
        return self._value

    @value.setter
    def value(self, new_value):
        self._value = new_value

    def setdefault(self, default=None):
        """If no value is set, set it to `default`.
        Then return the current value.
        """
        if self.value is None:
            self.value = default
        return self.value

    @classmethod
    def _is_secret(self, key):
        """Should the value of the given key be treated as secret?

        This will have to do, in the absence of programmatic ways of
        saying that a specific setting should be treated as secret.
        """
        return any(
            key == x or
            key.startswith('%s_' % x) or
            key.endswith('_%s' % x) or
            ("_%s_" % x) in key
            for x in ('secret', 'password')
        )

    @property
    def is_secret(self):
        """Should the value of this key be treated as secret?"""
        return self._is_secret(self.key)

    def value_or_default(self, default):
        """Return the value of this setting. If the value is None,
        set it to `default` and return that instead.
        """
        if self.value is None:
            self.value = default
        return self.value

    MEANS_YES = set(['true', 't', 'yes', 'y'])

    @property
    def bool_value(self):
        """Turn the value into a boolean if possible.

        :return: A boolean, or None if there is no value.
        """
        if self.value:
            if self.value.lower() in self.MEANS_YES:
                return True
            return False
        return None

    @property
    def int_value(self):
        """Turn the value into an int if possible.

        :return: An integer, or None if there is no value.

        :raise ValueError: If the value cannot be converted to an int.
        """
        if self.value:
            return int(self.value)
        return None

    @property
    def float_value(self):
        """Turn the value into an float if possible.

        :return: A float, or None if there is no value.

        :raise ValueError: If the value cannot be converted to a float.
        """
        if self.value:
            return float(self.value)
        return None

    @property
    def json_value(self):
        """Interpret the value as JSON if possible.

        :return: An object, or None if there is no value.

        :raise ValueError: If the value cannot be parsed as JSON.
        """
        if self.value:
            return json.loads(self.value)
        return None

# Join tables for many-to-many relationships


libraries_audiences = Table(
    'libraries_audiences', Base.metadata,
    Column('library_id', Integer, ForeignKey('libraries.id'), index=True, nullable=False),
    Column('audience_id', Integer, ForeignKey('audiences.id'), index=True, nullable=False),
    UniqueConstraint('library_id', 'audience_id'),
)


class Admin(Base):
    __tablename__ = 'admins'
    id = Column(Integer, primary_key=True)
    username = Column(Unicode, index=True, unique=True, nullable=False)
    password = Column(Unicode, index=True)

    @classmethod
    def make_password(cls, raw_password):
        return generate_password_hash(raw_password).decode('utf-8')

    def check_password(self, raw_password):
        return check_password_hash(self.password, raw_password)

    @classmethod
    def authenticate(cls, _db, username, password):
        """Finds an authenticated Admin by username and password
        :return: Admin or None
        """
        setting_up = _db.query(Admin).count() == 0
        admin, is_new = get_one_or_create(
            _db, Admin, username=username
        )
        if setting_up:
            admin.password = cls.make_password(password)
            return admin
        elif not is_new and admin and admin.check_password(password):
            return admin
        return None

    def __repr__(self):
        return "<Admin: username=%s>" % self.username
