"""Админка платформы (только суперадмин): одобрение/блокировка агентств, журнал аудита."""
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..models import AGENCY_STATUSES, Agency, AuditLog, Lead, User
from ..security import audit

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def _guard():
    if not g.is_platform:
        abort(404)
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    if not current_user.is_superadmin:
        abort(403)


@bp.route("/")
def index():
    order = db.case((Agency.status == "pending_review", 0), else_=1)
    agencies = Agency.query.order_by(order, Agency.created_at.desc()).all()
    stats = {a.id: Lead.query.filter_by(agency_id=a.id).count() for a in agencies}
    owners = {u.agency_id: u for u in User.query.filter(User.agency_id.isnot(None)).all()}
    log = AuditLog.query.order_by(AuditLog.id.desc()).limit(80).all()
    return render_template("admin/index.html", agencies=agencies, stats=stats, owners=owners, log=log, statuses=AGENCY_STATUSES)


@bp.route("/agencies/<int:agency_id>/status", methods=["POST"])
def set_status(agency_id):
    a = db.get_or_404(Agency, agency_id)
    status = request.form.get("status")
    if status not in AGENCY_STATUSES:
        abort(400)
    a.status = status
    if status == "suspended":
        a.site_published = False
    db.session.commit()
    audit("admin_set_status", agency_id=a.id, user_id=current_user.id, detail=status)
    flash(f"{a.name}: {status}", "success")
    return redirect(url_for("admin.index"))
