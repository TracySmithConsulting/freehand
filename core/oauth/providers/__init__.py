from .google import GoogleConnector
from .microsoft import MicrosoftConnector
from .zoom import ZoomConnector
from .facebook import FacebookConnector
from .instagram import InstagramConnector
from .github_pat import GitHubPATConnector
from .email import EmailConnector

CONNECTORS = {
    "google": GoogleConnector,
    "microsoft": MicrosoftConnector,
    "zoom": ZoomConnector,
    "facebook": FacebookConnector,
    "instagram": InstagramConnector,
    "github": GitHubPATConnector,
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
