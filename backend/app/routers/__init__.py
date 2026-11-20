"""业务路由汇总：语义证据固定路径优先注册，通用模块动态路由兜底。"""
from __future__ import annotations

from app.routers import evidence, resource

# evidence 含 /api/semantic-evidence/* 固定路径，必须放在 /api/{module} 之前，
# 否则会被动态资源路由抢先匹配成模块名。
ROUTERS = [evidence, resource]
