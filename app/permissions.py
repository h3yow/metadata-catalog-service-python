"""鉴权与授权：从请求头识别调用方，并判定操作权限。

本地服务采用简化身份协议：
- X-User:  用户 id（缺省 anonymous）
- X-Roles: 逗号分隔的角色列表，如 "admin,steward"

授权原则：
- 归属/标签/术语发布：表负责人或 admin/steward 角色；
- 路由先判断“资源是否存在”，再判断权限，从而把 404 与 403 区分开。
"""

from __future__ import annotations

from fastapi import Header

from .errors import ForbiddenError
from .models import Actor

PRIVILEGED_ROLES = ("admin", "steward")


def get_actor(
    x_user: str = Header(default="anonymous"),
    x_roles: str = Header(default=""),
) -> Actor:
    roles = [r.strip() for r in x_roles.split(",") if r.strip()]
    return Actor(user=x_user, roles=roles)


def is_privileged(actor: Actor) -> bool:
    return any(r in PRIVILEGED_ROLES for r in actor.roles)


def require_owner_or_role(
    actor: Actor,
    owner: str,
    roles: tuple[str, ...] = PRIVILEGED_ROLES,
) -> None:
    if actor.user == owner or any(r in roles for r in actor.roles):
        return
    raise ForbiddenError(
        f"user '{actor.user}' is neither owner '{owner}' nor a privileged role",
        details={"user": actor.user, "owner": owner, "required_roles": list(roles)},
    )
