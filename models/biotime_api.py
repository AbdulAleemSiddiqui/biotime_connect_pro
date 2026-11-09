# -*- coding: utf-8 -*-
"""Thin REST client for ZKTeco BioTime 8.x / 9.x / Cloud.

Authentication modes
  token : POST /api-token-auth/      -> Authorization: Token <t>   (BioTime 9 / ZKBioTime)
  jwt   : POST /jwt-api-token-auth/  -> Authorization: JWT <t>     (BioTime 8.x / Cloud)
  basic : HTTP Basic
"""
import logging

import requests

_logger = logging.getLogger(__name__)


class BioTimeError(Exception):
    """Raised for any connectivity / API problem."""


class BioTimeClient:
    def __init__(self, server):
        self.server = server
        self.base = server._base_url()
        self.session = requests.Session()
        self.session.verify = bool(server.verify_ssl)
        self.timeout = server.timeout or 30
        self.headers = {'Content-Type': 'application/json'}
        self._logged_in = False

    def _raw(self, method, path, **kw):
        url = self.base + path
        try:
            resp = self.session.request(
                method, url, headers=self.headers, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise BioTimeError("Cannot reach %s: %s" % (url, exc)) from exc
        if resp.status_code >= 400:
            raise BioTimeError("HTTP %s on %s: %s" % (resp.status_code, path, resp.text[:300]))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise BioTimeError("Invalid JSON from %s" % url) from exc

    def login(self):
        srv = self.server
        if srv.auth_type == 'basic':
            self.session.auth = (srv.username, srv.password)
        else:
            path = '/jwt-api-token-auth/' if srv.auth_type == 'jwt' else '/api-token-auth/'
            data = self._raw('POST', path, json={
                'username': srv.username, 'password': srv.password})
            token = data.get('token') if isinstance(data, dict) else None
            if not token:
                raise BioTimeError("Authentication failed: no token returned.")
            self.headers['Authorization'] = '%s %s' % (
                'JWT' if srv.auth_type == 'jwt' else 'Token', token)
        self._logged_in = True

    def request(self, method, path, params=None, json=None):
        if not self._logged_in:
            self.login()
        return self._raw(method, path, params=params, json=json)

    def paginate(self, path, params=None, page_size=100):
        """Yield one list of rows per page until the API reports no next page."""
        page = 1
        while True:
            query = dict(params or {})
            query.update(page=page, page_size=page_size)
            data = self.request('GET', path, params=query)
            if isinstance(data, list):
                yield data
                return
            rows = data.get('data', data.get('results')) or []
            yield rows
            if not data.get('next') or not rows:
                return
            page += 1
