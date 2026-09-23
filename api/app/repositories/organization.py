from __future__ import annotations

from ..core.clock import now
from ..models import (
    Lab, Membership, Organization, Project, ProjectMember, ServiceIdentity,
)
from .base import Repository, ScopedRepository


class OrganizationRepository(Repository[Organization]):
    model = Organization

    def list(self) -> list[Organization]:
        return list(self.db.query(Organization).order_by(Organization.code).all())

    def by_code(self, code: str) -> Organization | None:
        return self.db.query(Organization).filter(Organization.code == code).first()


class LabRepository(ScopedRepository[Lab]):
    model = Lab


class ProjectRepository(ScopedRepository[Project]):
    model = Project

    def restricted_ids(self) -> set[str]:
        return {p.id for p in self.query().filter(Project.restricted.is_(True)).all()}


class MembershipRepository(Repository[Membership]):
    model = Membership

    def active_for_user(self, user_id: str) -> list[Membership]:
        return list(
            self.db.query(Membership)
            .filter(Membership.user_id == user_id, Membership.state == "active")
            .order_by(Membership.granted_at)
            .all()
        )

    def find(self, org_id: str, user_id: str) -> Membership | None:
        return (
            self.db.query(Membership)
            .filter(Membership.org_id == org_id, Membership.user_id == user_id)
            .first()
        )

    def for_org(self, org_id: str) -> list[Membership]:
        return list(self.db.query(Membership).filter(Membership.org_id == org_id).all())


class ProjectMemberRepository(Repository[ProjectMember]):
    model = ProjectMember

    def project_ids_for(self, user_id: str) -> set[str]:
        rows = (
            self.db.query(ProjectMember.project_id)
            .filter(ProjectMember.user_id == user_id, ProjectMember.state == "active")
            .all()
        )
        return {row[0] for row in rows}


class ServiceIdentityRepository(Repository[ServiceIdentity]):
    model = ServiceIdentity

    def by_source(self, source: str) -> ServiceIdentity | None:
        return self.db.query(ServiceIdentity).filter(ServiceIdentity.source == source).first()

    def for_org(self, org_id: str) -> list[ServiceIdentity]:
        return list(
            self.db.query(ServiceIdentity)
            .filter(ServiceIdentity.org_id == org_id)
            .order_by(ServiceIdentity.source)
            .all()
        )

    def touch(self, identity: ServiceIdentity) -> None:
        identity.last_used_at = now()
