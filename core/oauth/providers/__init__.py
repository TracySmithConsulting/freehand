from .email import EmailConnector
from .facebook import FacebookConnector
from .instagram import InstagramConnector
from .slack import SlackConnector
from .zoom import ZoomConnector

# Google and Microsoft connect through OpenConnector (see
# core/tools/integrations.py OC_APPS), not this broker.
CONNECTORS = {
    "zoom": ZoomConnector,
    "facebook": FacebookConnector,
    "instagram": InstagramConnector,
    "email": EmailConnector,
    "slack": SlackConnector,
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
