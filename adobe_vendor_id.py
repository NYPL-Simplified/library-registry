import datetime
import flask
from flask import Response
from nose.tools import set_trace
import base64

from model import (
    ShortClientTokenDecoder,
)
from util.xmlparser import XMLParser

class AdobeVendorIDController(object):

    """Flask controllers that implement the Account Service and
    Authorization Service portions of the Adobe Vendor ID protocol.
    """
    def __init__(self, _db, vendor_id, node_value):
        self._db = _db
        self.request_handler = AdobeVendorIDRequestHandler(vendor_id)
        self.model = AdobeVendorIDModel(self._db, node_value)

    def signin_handler(self):
        """Process an incoming signInRequest document."""
        __transaction = self._db.begin_nested()
        output = self.request_handler.handle_signin_request(
            flask.request.data, self.model.standard_lookup,
            self.model.authdata_lookup)
        __transaction.commit()
        return Response(output, 200, {"Content-Type": "application/xml"})

    def userinfo_handler(self):
        """Process an incoming userInfoRequest document."""
        output = self.request_handler.handle_accountinfo_request(
            flask.request.data, self.model.urn_to_label)
        return Response(output, 200, {"Content-Type": "application/xml"})

    def status_handler(self):
        return Response("UP", 200, {"Content-Type": "text/plain"})


class AdobeRequestParser(XMLParser):

    NAMESPACES = { "adept" : "http://ns.adobe.com/adept" }

    def process(self, data):
        requests = list(self.process_all(
            data, self.REQUEST_XPATH, self.NAMESPACES))
        if not requests:
            return None
        # There should only be one request tag, but if there's more than
        # one, only return the first one.
        return requests[0]

    def _add(self, d, tag, key, namespaces, transform=None):
        v = self._xpath1(tag, 'adept:' + key, namespaces)
        if v is not None:
            v = v.text
            if v is not None:
                v = v.strip()
                if transform is not None:
                    v = transform(v)
        d[key] = v


class AdobeSignInRequestParser(AdobeRequestParser):

    REQUEST_XPATH = "/adept:signInRequest"

    STANDARD = 'standard'
    AUTH_DATA = 'authData'

    def process_one(self, tag, namespaces):
        method = tag.attrib.get('method')

        if not method:
            raise ValueError("No signin method specified")
        data = dict(method=method)
        if method == self.STANDARD:
            self._add(data, tag, 'username', namespaces)
            self._add(data, tag, 'password', namespaces)
        elif method == self.AUTH_DATA:
            self._add(data, tag, self.AUTH_DATA, namespaces, base64.b64decode)
        else:
            raise ValueError("Unknown signin method: %s" % method)
        return data


class AdobeAccountInfoRequestParser(AdobeRequestParser):

    REQUEST_XPATH = "/adept:accountInfoRequest"

    def process_one(self, tag, namespaces):
        method = tag.attrib.get('method')
        data = dict(method=method)
        self._add(data, tag, 'user', namespaces)
        return data


class AdobeVendorIDRequestHandler(object):

    """Standalone class that can be tested without bringing in Flask or
    the database schema.
    """

    SIGN_IN_RESPONSE_TEMPLATE = """<signInResponse xmlns="http://ns.adobe.com/adept">
<user>%(user)s</user>
<label>%(label)s</label>
</signInResponse>"""

    ACCOUNT_INFO_RESPONSE_TEMPLATE = """<accountInfoResponse xmlns="http://ns.adobe.com/adept">
<label>%(label)s</label>
</accountInfoResponse>"""

    AUTH_ERROR_TYPE = "AUTH"
    ACCOUNT_INFO_ERROR_TYPE = "ACCOUNT_INFO"   

    ERROR_RESPONSE_TEMPLATE = '<error xmlns="http://ns.adobe.com/adept" data="E_%(vendor_id)s_%(type)s %(message)s"/>'

    TOKEN_FAILURE = 'Incorrect token.'
    AUTHENTICATION_FAILURE = 'Incorrect barcode or PIN.'
    URN_LOOKUP_FAILURE = "Could not identify patron from '%s'."

    def __init__(self, vendor_id):
        self.vendor_id = vendor_id

    def handle_signin_request(self, data, standard_lookup, authdata_lookup):
        parser = AdobeSignInRequestParser()
        try:
            data = parser.process(data)
        except Exception, e:
            return self.error_document(self.AUTH_ERROR_TYPE, str(e))
        user_id = label = None
        if not data:
            return self.error_document(
                self.AUTH_ERROR_TYPE, "Request document in wrong format.")
        if not 'method' in data:
            return self.error_document(
                self.AUTH_ERROR_TYPE, "No method specified")
        if data['method'] == parser.STANDARD:
            user_id, label = standard_lookup(data)
            failure = self.AUTHENTICATION_FAILURE
        elif data['method'] == parser.AUTH_DATA:
            authdata = data[parser.AUTH_DATA]
            user_id, label = authdata_lookup(authdata)
            failure = self.TOKEN_FAILURE
        if user_id is None:
            return self.error_document(self.AUTH_ERROR_TYPE, failure)
        else:
            return self.SIGN_IN_RESPONSE_TEMPLATE % dict(
                user=user_id, label=label)

    def handle_accountinfo_request(self, data, urn_to_label):
        parser = AdobeAccountInfoRequestParser()
        label = None
        try:
            data = parser.process(data)
            if not data:
                return self.error_document(
                    self.ACCOUNT_INFO_ERROR_TYPE,
                    "Request document in wrong format.")
            if not 'user' in data:
                return self.error_document(
                    self.ACCOUNT_INFO_ERROR_TYPE,
                    "Could not find user identifer in request document.")
            label = urn_to_label(data['user'])
        except Exception, e:
            return self.error_document(
                self.ACCOUNT_INFO_ERROR_TYPE, str(e))

        if label:
            return self.ACCOUNT_INFO_RESPONSE_TEMPLATE % dict(label=label)
        else:
            return self.error_document(
                self.ACCOUNT_INFO_ERROR_TYPE,
                self.URN_LOOKUP_FAILURE % data['user']
            )

    def error_document(self, type, message):
        return self.ERROR_RESPONSE_TEMPLATE % dict(
            vendor_id=self.vendor_id, type=type, message=message)

class AdobeVendorIDModel(object):

    """Implement Adobe Vendor ID within the library registry's database
    model.
    """

    def __init__(self, _db, node_value, temporary_token_duration=None):
        self._db = _db
        self.temporary_token_duration = (
            temporary_token_duration or datetime.timedelta(minutes=10)
        )
        if isinstance(node_value, basestring):
            node_value = int(node_value, 16)
        self.short_client_token_decoder = ShortClientTokenDecoder(node_value)
        
    def standard_lookup(self, authorization_data):
        """Treat an incoming username and password as the two parts of a short
        client token. Return an Adobe Account ID and a human-readable
        label. Create a DelegatedPatronIdentifier to hold the Adobe
        Account ID if necessary.
        """
        username = authorization_data.get('username') 
        password = authorization_data.get('password')
        delegated_patron_identifier = self.short_client_token_decoder.decode_two_part(
            self._db, username, password
        )
        return self.account_id_and_label(delegated_patron_identifier)
        
    def authdata_lookup(self, authdata):
        """Treat an authdata string as a short client token. Return an Adobe
        Account ID and a human-readable label. Create a
        DelegatedPatronIdentifier to hold the Adobe Account ID if
        necessary.
        """
        delegated_patron_identifier = self.short_client_token_decoder.decode(
            self._db, authdata
        )
        return self.account_id_and_label(delegated_patron_identifier)

    def account_id_and_label(self, delegated_patron_identifier):
        "Turn a DelegatedPatronIdentifier into a (account id, label) 2-tuple."
        urn = delegated_patron_identifier.delegated_identifier
        return (urn, self.urn_to_label(urn))
        
    def urn_to_label(self, urn):
        """We have no information about patrons, so labels are sparse."""
        return "Delegated account ID %s" % urn
