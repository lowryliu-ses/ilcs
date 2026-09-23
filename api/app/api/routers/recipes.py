from fastapi import APIRouter

from ...schemas import GoldenBatchIn, RecipeCreateIn, RecipePatchIn, RecipeTransitionIn
from ...services.recipe_service import RecipeService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(prefix="/recipes", tags=["recipe"])


@router.get("")
def list_recipes(db: DbSession, ctx: Ctx):
    return RecipeService(db, ctx).list()


@router.get("/{recipe_id}")
def get_recipe(recipe_id: str, db: DbSession, ctx: Ctx):
    return RecipeService(db, ctx).get(recipe_id)


@router.post("", status_code=201)
def create_recipe(payload: RecipeCreateIn, db: DbSession, user: CurrentUser, ctx=require("recipe.edit")):
    return RecipeService(db, ctx).create(payload.name, payload.plate, payload.copy_from, user)


@router.patch("/{recipe_id}")
def patch_recipe(
    recipe_id: str, payload: RecipePatchIn, db: DbSession, user: CurrentUser,
    ctx=require("recipe.edit"),
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    return RecipeService(db, ctx).patch(recipe_id, changes, user)


@router.post("/{recipe_id}/submit")
def submit_recipe(recipe_id: str, db: DbSession, user: CurrentUser, ctx=require("recipe.submit")):
    return RecipeService(db, ctx).submit(recipe_id, user)


@router.post("/{recipe_id}/transition")
def transition_recipe(
    recipe_id: str, payload: RecipeTransitionIn, db: DbSession, user: CurrentUser, ctx: Ctx
):
    return RecipeService(db, ctx).transition_with_signature(
        recipe_id, payload.target_state, payload.signature_id, user
    )


@router.post("/{recipe_id}/revision")
def create_revision(recipe_id: str, db: DbSession, user: CurrentUser, ctx=require("recipe.edit")):
    return RecipeService(db, ctx).create_revision(recipe_id, user)


@router.post("/{recipe_id}/golden-batch")
def set_golden_batch(
    recipe_id: str, payload: GoldenBatchIn, db: DbSession, user: CurrentUser,
    ctx=require("golden.set"),
):
    return RecipeService(db, ctx).set_golden_batch(
        recipe_id, payload.batch_id, payload.signature_id, user
    )


@router.delete("/{recipe_id}")
def delete_recipe(recipe_id: str, db: DbSession, user: CurrentUser, ctx=require("recipe.edit")):
    """只删草稿。出过批次、被方案引用或派生过修订的一律拒绝。"""
    return RecipeService(db, ctx).delete(recipe_id, user)
