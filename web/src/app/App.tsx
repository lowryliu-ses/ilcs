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
import { ExceptionsPage } from '../features/exceptions/ExceptionsPage';
import { IntegrationsPage } from '../features/integrations/IntegrationsPage';
import { MethodsPage } from '../features/methods/MethodsPage';
import { EnvironmentPage } from '../features/environment/EnvironmentPage';
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
   点进去看到空页面比没有这一项更糟。
   分组与组内顺序按业务主线：方案 → 流程 → 接样建批 → 排程执行 → 数据审核 → 报告。
   菜单名与页面标题保持一致。 */
type NavItem = { path: string; label: string; perm?: string };
const NAV: [string, NavItem[]][] = [
  ['工作台', [
    { path: '/dashboard', label: '工作台' },
    { path: '/tasks', label: '任务中心' },
  ]],
  ['实验设计', [
    { path: '/plans', label: '实验方案' },
    { path: '/recipes', label: '实验流程' },
    { path: '/methods', label: '设备方法' },
    { path: '/sops', label: 'SOP 规程' },
  ]],
  ['执行与监控', [
    { path: '/samples', label: '样本管理' },
    { path: '/batches', label: '批次管理' },
    { path: '/schedule', label: '排程' },
    { path: '/floor', label: '现场监控' },
    { path: '/alarms', label: '报警处理' },
    { path: '/exceptions', label: '异常处理' },
  ]],
  ['数据与报告', [
    { path: '/data-review', label: '数据审核' },
    { path: '/results', label: '结果分析' },
    { path: '/reports', label: '报告管理' },
    { path: '/metrics', label: '指标与规则' },
  ]],
  ['资源管理', [
    { path: '/assets', label: '仪器设备' },
    { path: '/stations', label: '工位配置' },
    { path: '/materials', label: '试剂耗材' },
    { path: '/people', label: '人员与资质' },
    { path: '/environment', label: '环境监测' },
  ]],
  ['系统管理', [
    { path: '/governance', label: '用户与权限', perm: 'service.manage' },
    { path: '/integrations', label: '集成与通知', perm: 'integration.manage' },
    { path: '/audit', label: '审计日志', perm: 'audit.read' },
  ]],
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
    '/exceptions': counts?.open_exceptions,
    '/schedule': (counts?.planned ?? 0) + (counts?.pending_proposals ?? 0),
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
  const hot = new Set(['/alarms', '/exceptions', '/people', '/materials', '/assets']);

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <b>ILCS</b>
          <span>实验室平台</span>
        </div>
        <nav>
          {NAV.map(([group, items]) => {
            const visible = items.filter((item) => !item.perm || user.perms.includes(item.perm));
            if (!visible.length) return null;
            return (
              <div key={group}>
                <div className="nav-group">{group}</div>
                {visible.map(({ path, label }) => (
                  <NavLink key={path} to={path} className={({ isActive }) => (isActive ? 'active' : '')}>
                    {label}
                    {badges[path] ? (
                      <span className={`count${hot.has(path) ? ' hot' : ''}`}>{badges[path]}</span>
                    ) : null}
                  </NavLink>
                ))}
              </div>
            );
          })}
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
          <Route path="/exceptions" element={<ExceptionsPage />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="/methods" element={<MethodsPage />} />
          <Route path="/environment" element={<EnvironmentPage />} />
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
