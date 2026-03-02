"""Shared HTML login form parser for Keycloak and Gluu/oxAuth."""

from html.parser import HTMLParser


class LoginFormParser(HTMLParser):
    """Extract form action URL, hidden fields, and username/password field names."""

    def __init__(self, form_id=None):
        super().__init__()
        self.action = None
        self.fields = {}
        self.username_field = None
        self.password_field = None
        self.submit_fields = []
        self._in_form = False
        self._form_id = form_id  # e.g. "kc-form-login" for Keycloak

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            # Match by specific ID if given, otherwise any POST form
            if self._form_id and attrs.get("id") == self._form_id:
                self._in_form = True
                self.action = attrs.get("action", "")
            elif not self._form_id and attrs.get("method", "").lower() == "post":
                self._in_form = True
                self.action = attrs.get("action", "")
            elif not self._in_form and attrs.get("method", "").lower() == "post":
                self._in_form = True
                self.action = attrs.get("action", "")
            return
        if not self._in_form:
            return
        if tag == "input":
            name = attrs.get("name", "")
            input_type = attrs.get("type", "text").lower()
            value = attrs.get("value", "")
            if not name:
                return
            if input_type == "hidden":
                self.fields[name] = value
            elif input_type in ("text", "email"):
                self.username_field = name
            elif input_type == "password":
                self.password_field = name
            elif input_type == "submit":
                self.submit_fields.append((name, value))
        elif tag == "button":
            name = attrs.get("name", "")
            if name and attrs.get("type", "submit").lower() == "submit":
                self.submit_fields.append((name, attrs.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
