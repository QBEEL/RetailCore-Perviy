"""Доступ через собственный сервер без прав администратора."""
from . import subscription
from .manager import (
    VpnError, VpnManager, load_link, load_subscription, save_link, save_subscription,
)
from .vless_uri import VlessError, VlessLink, parse

__all__ = [
    "VlessError", "VlessLink", "VpnError", "VpnManager", "load_link", "load_subscription",
    "parse", "save_link", "save_subscription", "subscription",
]
