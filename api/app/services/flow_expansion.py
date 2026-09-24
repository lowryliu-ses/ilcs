"""子流程展开的服务层接线：按当前组织取被引用的方法，交给 `domain/subflow.py` 展开。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..domain.subflow import Resolver, SubflowRecipe, expand
from ..repositories.recipes import RecipeRepository


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
    """方法展开后的步骤与子方法带来的 BOM。引用有问题时抛 SubflowError。"""
    return expand(recipe.steps or [], resolver(db, ctx), (recipe.id,))
