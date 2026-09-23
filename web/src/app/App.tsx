import { useEffect } from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';

import { AlarmsPage } from '../features/alarms/AlarmsPage';
import { AssetsPage } from '../features/assets/AssetsPage';
import { AuditPage } from '../features/audit/AuditPage';
import { BatchDetailPage } from '../features/batches/BatchDetailPage';
import { BatchesPage } from '../features/batches/BatchesPage';
import { DashboardPage } from '../features/dashboard/DashboardPage';
import { FloorPage } from '../features/floor/FloorPage';
import { DataReviewPage } from '../features/data-review/DataReviewPage';
import { MaterialsPage } from '../features/materials/MaterialsPage';
import { MetricsPage } from '../features/metrics/MetricsPage';
import { GovernancePage } from '../features/governance/GovernancePage';
import { PeoplePage } from '../features/people/PeoplePage';
import { PlanDetailPage } from '../features/plans/PlanDetailPage';
import { PlansPage } from '../features/plans/PlansPage';
import { RecipeDetailPage } from '../features/recipes/RecipeDetailPage';
import { RecipeEditorPage } from '../features/recipes/RecipeEditorPage';
import { RecipesPage } from '../features/recipes/RecipesPage';
import { ReportsPage } from '../features/reports/ReportsPage';
import { ResultsPage } from '../features/results/ResultsPage';
import { SampleDetailPage, SamplesPage } from '../features/samples/SamplesPage';
import { SchedulePage } from '../features/schedule/SchedulePage';
import { SopsPage } from '../features/sops/SopsPage';
import { StationsPage } from '../features/stations/StationsPage';
import { TasksPage } from '../features/tasks/TasksPage';
import { LoginPage } from '../features/identity/LoginPage';
import { ChangePasswordPage } from '../features/identity/ChangePasswordPage';
import { api } from '../shared/api';
import { useLive, useQuery } from '../shared/query';
import { startStream } from '../shared/stream';
import { useSession } from '../shared/session';
import type { Dashboard, Gate } from '../shared/types';

/* 导航只列已实现的入口。未来才有的功能不作为可点的菜单出现——
   点进去看到空页面比没有这一项更糟。 */
const NAV: [string, [string, string][]][] = [
  ['概览', [['/dashboard', '工作台']]],
  ['设计', [['/plans', '实验方案'], ['/recipes', '方法与配方'], ['/sops', 'SOP']]],
  ['执行', [['/floor', '现场总览'], ['/tasks', '任务中心'], ['/schedule', '排程'], ['/batches', '批次'], ['/alarms', '报警中心']]],
  ['科学数据', [['/samples', '样本中心'], ['/data-review', '数据审核'], ['/results', '结果分析'], ['/reports', '报告']]],
  ['资源', [['/assets', '仪器设备'], ['/stations', '工位与能力'], ['/materials', '试剂耗材'], ['/people', '人员与资质']]],
  ['治理', [['/metrics', '指标定义'], ['/audit', '审计记录'], ['/governance', '系统治理']]],
];

export function App() {
  const { user, ready, logout } = useSession();
  const active = !!user && !user.must_change_password;
  const gate = useQuery<Gate>(active ? 'gate' : null, () => api.get<Gate>('/gate'), 15000);
  const counters = useQuery<Dashboard>(active ? 'dashboard:nav' : null, () => api.get<Dashboard>('/dashboard'), 20000);
  const { live } = useLive();
  useEffect(() => (active ? startStream() : undefined), [active]);

  if (!ready) return <div className="boot">加载中…</div>;
  if (!user) return <LoginPage />;
  if (user.must_change_password) return <ChangePasswordPage />;

  const counts = counters.data?.counts;
  const badges: Record<string, number | undefined> = {
    '/alarms': counts?.open_alarms,
    '/schedule': counts?.planned,
    '/recipes': counts?.recipes_in_review,
    '/plans': counts?.plans_in_review,
    '/batches': counts?.held,
    '/tasks': (counts?.my_tasks ?? 0) + (counts?.pending_accept ?? 0),
    '/data-review': counts?.pending_result_reviews,
    '/reports': counts?.report_reviews,
    '/people': counts?.expiring_qualifications,
    '/assets': counts?.unavailable_assets,
    '/materials': counts?.expiring_lots,
  };
  const hot = new Set(['/alarms', '/people', '/materials', '/assets']);

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <b>ILCS</b>
          <span>实验室平台</span>
        </div>
        <nav>
          {NAV.map(([group, items]) => (
            <div key={group}>
              <div className="nav-group">{group}</div>
              {items.filter(([path]) => path !== '/governance' || user.perms.includes('service.manage')).map(([path, label]) => (
                <NavLink key={path} to={path} className={({ isActive }) => (isActive ? 'active' : '')}>
                  {label}
                  {badges[path] ? (
                    <span className={`count${hot.has(path) ? ' hot' : ''}`}>{badges[path]}</span>
                  ) : null}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
      </aside>

      <header className="topbar">
        <div className="status">
          <span>
            <span className={`dot ${gate.data?.open ? 'ok' : 'bad'}`} />
            {gate.data?.open ? '执行引擎在线' : '控制已锁定'}
          </span>
          <span>
            运行中 <b>{counts?.running ?? 0}</b> · 保持 <b>{counts?.held ?? 0}</b>
          </span>
          <span>
            我的待办 <b>{counts?.my_tasks ?? 0}</b> · 待复核 <b>{counts?.pending_result_reviews ?? 0}</b>
          </span>
          <span>
            工位在线 <b>{counts?.stations_online ?? 0}/{counts?.stations_total ?? 0}</b>
          </span>
          <span>
            结果未知指令 <b>{counts?.unknown_commands ?? 0}</b>
          </span>
          <span className={counts?.open_alarms ? 'alarm-hot' : ''}>
            未确认报警 <b>{counts?.open_alarms ?? 0}</b>
          </span>
          <span title={live ? '服务端变更实时推送，轮询仅作兜底' : '推送未连接，按页面轮询间隔刷新'}>
            <span className={`dot ${live ? 'ok' : 'warn'}`} />
            {live ? '实时' : '轮询'}
          </span>
        </div>
        <div className="who">
          <span className="avatar">{user.display_name.slice(0, 1)}</span>
          <div>
            <b>{user.display_name}</b>
            <div className="tiny muted">
              {user.role_name}
              {user.organization_name ? ` · ${user.organization_name}` : ''}
            </div>
          </div>
          <button className="btn sm" onClick={logout}>
            退出
          </button>
        </div>
      </header>

      <main className="view">
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/plans/:planId" element={<PlanDetailPage />} />
          <Route path="/recipes" element={<RecipesPage />} />
          <Route path="/recipes/:recipeId" element={<RecipeDetailPage />} />
          <Route path="/recipes/:recipeId/edit" element={<RecipeEditorPage />} />
          <Route path="/sops" element={<SopsPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/floor" element={<FloorPage />} />
          <Route path="/schedule" element={<SchedulePage />} />
          <Route path="/batches" element={<BatchesPage />} />
          <Route path="/batches/:batchId" element={<BatchDetailPage />} />
          <Route path="/samples" element={<SamplesPage />} />
          <Route path="/samples/:sampleId" element={<SampleDetailPage />} />
          <Route path="/data-review" element={<DataReviewPage />} />
          <Route path="/results" element={<ResultsPage />} />
          <Route path="/results/:batchId" element={<ResultsPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/assets" element={<AssetsPage />} />
          <Route path="/stations" element={<StationsPage />} />
          <Route path="/materials" element={<MaterialsPage />} />
          <Route path="/people" element={<PeoplePage />} />
          <Route path="/alarms" element={<AlarmsPage />} />
          <Route path="/metrics" element={<MetricsPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/governance" element={<GovernancePage />} />
        </Routes>
      </main>
    </div>
  );
}
