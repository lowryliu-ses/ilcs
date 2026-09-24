"""子流程展开的服务层接线：按当前组织取被引用的方法，交给 `domain/subflow.py` 展开。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..domain.methods import apply as apply_methods
from ..domain.steps import normalize
from ..domain.subflow import Resolver, SubflowRecipe, expand
from ..repositories.recipes import RecipeRepository
from .method_service import resolver as method_resolver


def resolver(db: Session, ctx: AccessContext) -> Resolver:
    recipes = RecipeRepository(db, ctx)

    def resolve(recipe_id: str) -> SubflowRecipe | None:
        recipe = recipes.get(recipe_id)
        if recipe is None:
            return None
        return SubflowRecipe(
            id=recipe.id, name=recipe.name, version=recipe.version, state=recipe.state,
            steps=list(recipe.steps or []), bom=list(recipe.bom or []),
            needs_revision=bool(recipe.needs_revision),
        )

    return resolve


def expanded_steps(db: Session, ctx: AccessContext, recipe) -> tuple[list[dict], list[dict]]:
    """方法展开后的步骤与子方法带来的 BOM。引用有问题时抛 SubflowError。

    子流程展开之后再解析设备方法引用（子方法里的步骤也可能引用设备方法）：补缺省参数、写入方法快照。
    方法引用的问题不在这里抛——校验与建批次各自按 `method_problems` 裁决。
    """
    steps, bom = expand(recipe.steps or [], resolver(db, ctx), (recipe.id,))
    applied, _ = apply_methods(steps, method_resolver(db, ctx))
    return applied, bom


def resolved_steps(db: Session, ctx: AccessContext, steps: list[dict]) -> tuple[list[dict], dict[str, list[str]]]:
    """只解析设备方法引用（不展开子流程）：给方法校验与编辑器提示用。"""
    return apply_methods(normalize(steps or []), method_resolver(db, ctx))
