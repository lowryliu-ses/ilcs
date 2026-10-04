"""可选模块的启用状态随登录资料下发，界面据此决定出不出对应菜单。"""


def test_profile_lists_enabled_modules(researcher):
    profile = researcher.get("/api/auth/me")
    assert profile.status_code == 200
    assert profile.json()["modules"] == ["formulation"]
