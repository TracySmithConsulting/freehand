from .email import EmailConnector
from .facebook import FacebookConnector

# Google, Microsoft, Zoom, Instagram and Slack connect through
# OpenConnector (see core/tools/integrations.py OC_APPS and the oc_* tools),
# not this broker. The rule: if OpenConnector has an app, it comes out of
# the broker. Facebook and email (IMAP/SMTP) stay: OpenConnector has
# neither.
CONNECTORS = {
    "facebook": FacebookConnector,
    "email": EmailConnector,
}


def get_connector(service: str):
    cls = CONNECTORS.get(service)
    if cls:
        return cls()
    return None


def get_all_providers():
    providers = []
    for service, cls in CONNECTORS.items():
        inst = cls()
        providers.append({
            "service": service,
            "name": inst.name,
            "icon": inst.icon,
            "scopes_read": inst.scopes_read,
            "scopes_write": inst.scopes_write,
            "supports_multi_account": inst.supports_multi_account,
        })
    return providers
