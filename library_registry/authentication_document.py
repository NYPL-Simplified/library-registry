import json
from collections import defaultdict

from flask_babel import lazy_gettext as lgt
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.orm.session import Session

from library_registry.constants import (AUTHENTICATION_DOCUMENT_MEDIA_TYPE, OPDS_CATALOG_MEDIA_TYPE)
from library_registry.model_helpers import get_one_or_create
from library_registry.model import (Audience, CollectionSummary, Place, ServiceArea)
from library_registry.problem_details import INVALID_INTEGRATION_DOCUMENT


class AuthenticationDocument:
    """
    Parse an Authentication For OPDS document, including the Library Simplified-specific extensions,
    extracting all the information of interest to the library registry.
    """
    ##### Class Constants ####################################################  # noqa: E266
    ANONYMOUS_ACCESS_REL        = "https://librarysimplified.org/rel/auth/anonymous"    # noqa: E221
    AUTHENTICATION_DOCUMENT_REL = "http://opds-spec.org/auth/document"                  # noqa: E221
    MEDIA_TYPE                  = AUTHENTICATION_DOCUMENT_MEDIA_TYPE                    # noqa: E221
    COVERAGE_EVERYWHERE         = "everywhere"                                          # noqa: E221
    PUBLIC_AUDIENCE             = 'public'                                              # noqa: E221

    AUDIENCES = [
        PUBLIC_AUDIENCE,
        'educational-primary',
        'educational-secondary',
        'research',
        'print-disability',
        'other',
    ]

    SIMPLYE_COLOR_SCHEMES = [       # The list of color schemes supported by SimplyE.
        "red",
        "blue",
        "gray",
        "gold",
        "green",
        "teal",
        "purple",
    ]

    ##### Public Interface / Magic Methods ###################################  # noqa: E266
    def __init__(self, _db, id, title, authentication, service_description,
                 color_scheme, collection_size, public_key, audiences,
                 service_area, focus_area, links, place_class=Place):
        self.id = id
        self.title = title
        self.authentication = authentication
        self.service_description = service_description
        self.color_scheme = color_scheme
        self.collection_size = collection_size
        self.public_key = public_key
        self.audiences = audiences or [self.PUBLIC_AUDIENCE]

        (self.service_area, self.focus_area) = self.parse_service_and_focus_area(
            _db, service_area, focus_area, place_class
        )

        self.links = links
        self.website = self.extract_link(rel="alternate", require_type="text/html")
        self.online_registration = self.has_link(rel="register")
        self.root = self.extract_link(rel="start", prefer_type=OPDS_CATALOG_MEDIA_TYPE)

        logo = self.extract_link(rel="logo")
        self.logo = None
        self.logo_link = None

        if logo:
            data = logo.get('href', '')
            if data and data.startswith('data:'):
                self.logo = data
            else:
                self.logo_link = logo

        self.anonymous_access = False

        for flow in self.authentication_flows:
            if flow.get('type') == self.ANONYMOUS_ACCESS_REL:
                self.anonymous_access = True
                break

    def extract_link(self, rel, require_type=None, prefer_type=None):
        """
        Find a link with the given link relation in the main authentication document.

        Does not consider links found in the authentication flows.

        :param rel: The link must use this as the link relation.
        :param require_type: The link must have this as its type.
        :param prefer_type: A link with this type is better than a link of some other type.
        """
        return self._extract_link(self.links, rel, require_type, prefer_type)

    def has_link(self, rel):
        """
        Is there a link with this link relation anywhere in the document?

        This checks both the main document and the authentication flows.

        :rel: The link must have this link relation.
        :return: True if there is a link with the link relation in the document, False otherwise.
        """
        if self._extract_link(self.links, rel):
            return True

        # We couldn't find a matching link in the main set of links, but maybe there's a matching
        # link associated with a particular authentication flow.
        for flow in self.authentication_flows:
            if self._extract_link(flow.get('links', []), rel):
                return True

        return False

    def update_library(self, library):
        """
        Modify a library to reflect the current state of this AuthenticationDocument.

        :param library: A Library.
        :return: A ProblemDetail if there's a problem, otherwise None.
        """
        library.name = self.title
        library.description = self.service_description
        library.online_registration = self.online_registration
        library.anonymous_access = self.anonymous_access

        problem = self.update_audiences(library)

        if not problem:
            problem = self.update_service_areas(library)

        if not problem:
            problem = self.update_collection_size(library)

        return problem

    def update_audiences(self, library):
        return self._update_audiences(library, self.audiences)

    def update_collection_size(self, library):
        return self._update_collection_size(library, self.collection_size)

    def update_service_areas(self, library):
        """Update a library's ServiceAreas based on the contents of this document"""
        return self.set_service_areas(library, self.service_area, self.focus_area)

    ##### Private Methods ####################################################  # noqa: E266

    ##### Properties and Getters/Setters #####################################  # noqa: E266
    @property
    def authentication_flows(self):
        """Return all valid authentication flows in this document."""
        for i in self.authentication:
            if not isinstance(i, dict):
                continue    # Not a valid authentication flow.

            yield i

    ##### Class Methods ######################################################  # noqa: E266
    @classmethod
    def parse_service_and_focus_area(cls, _db, service_area, focus_area, place_class=Place):
        if service_area:
            service_area = cls.parse_coverage(_db, service_area, place_class=place_class)
        else:
            service_area = [place_class.everywhere(_db)], {}, {}

        if focus_area:
            focus_area = cls.parse_coverage(_db, focus_area, place_class=place_class)
        else:
            focus_area = service_area

        return service_area, focus_area

    @classmethod
    def parse_coverage(cls, _db, coverage, place_class=Place):
        """
        Derive Place objects from an Authentication For OPDS coverage object
        (i.e. a value for `service_area` or `focus_area`)

        :param coverage: An Authentication For OPDS coverage object.

        :param place_class: In unit tests, pass in a mock replacement for the Place class here.

        :return: A 3-tuple (places, unknown, ambiguous).

        `places` is a list of Place model objects.

        `unknown` is a coverage object representing the subset of `coverage` that had no corresponding
        Place objects. This object will not be used for any purpose except error display.

        `ambiguous` is a coverage object representing the subset of `coverage` that had more than one
        corresponding Place object. This object will not be used for any purpose except error display.
        """
        place_objs = []
        unknown = defaultdict(list)
        ambiguous = defaultdict(list)

        if coverage == cls.COVERAGE_EVERYWHERE:     # This library covers the entire universe! No need to parse.
            place_objs.append(place_class.everywhere(_db))
            coverage = {}  # Do no more processing
        elif not isinstance(coverage, dict):
            # The coverage is not in { nation: place } format.
            # Convert it into that format using the default nation.
            default_nation = place_class.default_nation(_db)

            if default_nation:
                coverage = {default_nation.abbreviated_name: coverage}
            else:
                # Oops, that's not going to work. We don't know which nation this place is in.
                # Return a coverage object that makes it semi-clear what the problem is.
                unknown["??"] = coverage
                coverage = {}   # Do no more processing

        for nation, places in list(coverage.items()):
            try:
                nation_obj = place_class.lookup_one_by_name(_db, nation, place_type=Place.NATION)

                if places == cls.COVERAGE_EVERYWHERE:   # This library covers an entire nation.
                    place_objs.append(nation_obj)
                else:                                   # This library covers a list of places within a nation.
                    if isinstance(places, str):
                        # This is invalid -- you're supposed to always pass in a list -- but we can support it.
                        places = [places]

                    for place in places:
                        try:
                            place_obj = nation_obj.lookup_inside(place)

                            if place_obj:                       # We found it.
                                place_objs.append(place_obj)
                            else:                               # We couldn't find any place with this name.
                                unknown[nation].append(place)
                        except MultipleResultsFound:
                            ambiguous[nation].append(place)     # The place was ambiguously named.

            except MultipleResultsFound:
                ambiguous[nation] = places                      # A nation was ambiguously named; not very likely.
            except NoResultFound:
                unknown[nation] = places                        # Unrecognized nation or we have no geography for it.

        return place_objs, unknown, ambiguous

    @classmethod
    def from_string(cls, _db, s, place_class=Place):
        data = json.loads(s)
        return cls.from_dict(_db, data, place_class)

    @classmethod
    def from_dict(cls, _db, data, place_class=Place):
        return AuthenticationDocument(
            _db,
            id=data.get('id', None),
            title=data.get('title', data.get('name', None)),
            authentication=data.get('authentication', []),
            service_description=data.get('service_description', None),
            color_scheme=data.get('color_scheme'),
            collection_size=data.get('collection_size'),
            public_key=data.get('public_key'),
            audiences=data.get('audience'),
            service_area=data.get('service_area'),
            focus_area=data.get('focus_area'),
            links=data.get('links', []),
            place_class=place_class
        )

    @classmethod
    def set_service_areas(cls, library, service_area, focus_area):
        """Replace a library's ServiceAreas with specific new values"""
        service_areas = []

        list(library.service_areas)

        empty = [[], {}, {}]    # What service_area or focus_area looks like when no input was specified.

        if focus_area == empty and service_area == empty:
            # A library can't lose its entire coverage area -- it's more likely that the coverage area was
            # grandfathered in and it just isn't set on the remote side.
            return

        if (focus_area == empty and service_area != empty or service_area == focus_area):
            # Service area and focus area are the same, either because they were defined that way explicitly or
            # because focus area was not specified.
            #
            # Register the service area as the focus area and call it a day.
            problem = cls._update_service_areas(library, service_area, ServiceArea.FOCUS, service_areas)

            if problem:
                return problem
        else:
            # Service area and focus area are different.
            problem = cls._update_service_areas(library, service_area, ServiceArea.ELIGIBILITY, service_areas)

            if problem:
                return problem

            problem = cls._update_service_areas(library, focus_area, ServiceArea.FOCUS, service_areas)

            if problem:
                return problem

        # Delete ServiceAreas associated with the given library which are not mentioned in the list we just gathered.
        library.service_areas = service_areas

    ##### Private Class Methods ##############################################  # noqa: E266
    @classmethod
    def _update_collection_size(self, library, sizes):
        if isinstance(sizes, str) or isinstance(sizes, int):
            sizes = {None: sizes}   # A single collection with no known language.

        if sizes is None:
            sizes = {}              # No collections are specified.

        if not isinstance(sizes, dict):
            return INVALID_INTEGRATION_DOCUMENT.detailed(
                lgt("'collection_size' must be a number or an object mapping language codes to numbers")
            )

        new_collections = set()
        unknown_size = 0

        try:
            for language, size in list(sizes.items()):
                summary = CollectionSummary.set(library, language, size)
                if summary.language is None:
                    unknown_size += summary.size
                new_collections.add(summary)
            if unknown_size:
                # We found one or more collections in languages we didn't recognize. Set the total size of
                # this collection as the size of a collection with unknown language.
                new_collections.add(CollectionSummary.set(library, None, unknown_size))

        except ValueError as e:
            return INVALID_INTEGRATION_DOCUMENT.detailed(str(e))

        # Destroy any CollectionSummaries representing collections no longer associated with this library.
        library.collections = list(new_collections)

    @classmethod
    def _update_service_areas(cls, library, areas, type, service_areas):
        """
        Update a Library's ServiceAreas with a new set based on `areas`.

        :param library: A Library.
        :param areas: A list [place_objs, unknown, ambiguous] of the sort returned by `parse_coverage()`.
        :param type: A value to use for `ServiceAreas.type`.
        :param service_areas: All ServiceAreas that became associated with the Library will be inserted into this list.

        :return: A ProblemDetailDocument if any of the service areas could not be transformed into Place objects.
                 Otherwise, None.
        """
        _db = Session.object_session(library)
        places, unknown, ambiguous = areas
        if unknown or ambiguous:
            msgs = []
            if unknown:
                msgs.append(str(lgt(f"The following service area was unknown: {json.dumps(unknown)}.")))
            if ambiguous:
                msgs.append(str(lgt(f"The following service area was ambiguous: {json.dumps(ambiguous)}.")))
            return INVALID_INTEGRATION_DOCUMENT.detailed(" ".join(msgs))

        for place in places:
            (service_area, _) = get_one_or_create(
                _db, ServiceArea, library_id=library.id, place_id=place.id, type=type
            )
            service_areas.append(service_area)

    @classmethod
    def _update_audiences(self, library, audiences):
        if not audiences:
            audiences = [Audience.PUBLIC]

        if isinstance(audiences, str):
            audiences = [audiences]     # This is invalid but we can easily support it.

        if not isinstance(audiences, list):
            return INVALID_INTEGRATION_DOCUMENT.detailed(lgt(f"'audience' must be a list: {audiences}"))

        filtered_audiences = set()      # Unrecognized audiences become Audience.OTHER.

        for audience in audiences:
            if audience in Audience.KNOWN_AUDIENCES:
                filtered_audiences.add(audience)
            else:
                filtered_audiences.add(Audience.OTHER)

        audiences = filtered_audiences

        audience_objs = []
        _db = Session.object_session(library)

        for audience in audiences:
            audience_obj = Audience.lookup(_db, audience)
            audience_objs.append(audience_obj)

        library.audiences = audience_objs

    @classmethod
    def _extract_link(cls, links, rel, require_type=None, prefer_type=None):
        if require_type and prefer_type:
            raise ValueError("At most one of require_type and prefer_type may be specified.")

        if not links:
            return None     # There are no links, period.

        good_enough = None

        if not isinstance(links, list):
            return          # Invalid links object; ignore it.

        for link in links:
            if rel != link.get('rel'):
                continue

            if not require_type and not prefer_type:
                return link     # Any link with this relation will work. Return the first one we see.

            # Beyond this point, either require_type or prefer_type is set, so the type of the link becomes relevant.
            link_type = link.get('type', '')

            if link_type and (
                                require_type and link_type.startswith(require_type)
                                or
                                prefer_type and link_type.startswith(prefer_type)
            ):
                # If we have a require_type, this means we have met the requirement. If we have a prefer_type,
                # we will not find a better link than this one. Return it immediately.
                return link

            if not require_type and not good_enough:
                # We would prefer a link of a certain type, but if it turns out there is no such link,
                # we will accept the first link of the given type.
                good_enough = link

        return good_enough
