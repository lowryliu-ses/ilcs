from . import (
    admin, alarms, analysis, assets, auth, batches, files, governance, materials, metrics, people,
    plans, recipes, reports, results, runtime, samples, schedule, sops, stations, steps, tasks,
)

ROUTERS = [
    auth.router,
    admin.router,
    governance.router,
    people.router,
    assets.router,
    recipes.router,
    plans.router,
    tasks.router,
    batches.router,
    steps.router,
    schedule.router,
    stations.router,
    runtime.router,
    materials.router,
    samples.router,
    metrics.router,
    analysis.router,
    alarms.router,
    results.router,
    sops.router,
    reports.router,
    files.router,
]
