import { useEffect, useMemo, useState } from "react";
import { StatusDot } from "./components";
import { AccountPage } from "./pages/AccountPage";
import { DataPage } from "./pages/DataPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RiskPage } from "./pages/RiskPage";
import { StrategiesPage } from "./pages/StrategiesPage";
import { routeFor, routes } from "./routes";
import { useControlData } from "./useControlData";

function App() {
  const {
    dashboard,
    operations,
    system,
    ledger,
    loading,
    refreshing,
    error,
    refresh,
    syncAccount,
    recordStrategyFill,
    confirmDividend,
    acknowledgeQueue,
    resolveQueue,
    setManualHalt,
    selectStrategy,
    createPaperIntent,
  } = useControlData();
  const [pathname, setPathname] = useState(window.location.pathname);
  const currentRoute = useMemo(() => routeFor(pathname), [pathname]);

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function navigate(path: string) {
    if (path === pathname) return;
    window.history.pushState({}, "", path);
    setPathname(path);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand-mark" href="/" onClick={(event) => { event.preventDefault(); navigate("/"); }}>DVL</a>
        <nav aria-label="主要導覽">
          {routes.map((route) => (
            <a
              href={route.path}
              key={route.path}
              className={`nav-item ${currentRoute.path === route.path ? "active" : ""}`}
              aria-current={currentRoute.path === route.path ? "page" : undefined}
              onClick={(event) => { event.preventDefault(); navigate(route.path); }}
            >
              <span className="nav-short">{route.short}</span><span className="nav-label">{route.label}</span>
            </a>
          ))}
        </nav>
        <div className="paper-badge">PAPER · READ ONLY</div>
      </aside>

      <main>
        <div className="system-strip">
          <span><StatusDot state={system?.database === "ok" ? "ready" : "blocked"} />資料庫 {system?.database ?? "connecting"}</span>
          <span><StatusDot state={system?.shioaji_secret_present ? "ready" : "required"} />Shioaji {system?.shioaji_mode ?? "—"}</span>
          <strong>正式下單 OFF</strong>
          <button type="button" onClick={() => void refresh()} disabled={refreshing}>{refreshing ? "讀取中" : "重新整理"}</button>
        </div>

        {error && <section className="critical-banner">無法取得營運資料：{error}</section>}
        {loading && !operations && <section className="loading-state">正在建立營運狀態，尚未使用任何預設值…</section>}

        {!loading && dashboard && operations && system && (
          currentRoute.path === "/" ? <OverviewPage operations={operations} dashboard={dashboard} onAcknowledge={acknowledgeQueue} onResolve={resolveQueue} onCreatePaper={createPaperIntent} />
            : currentRoute.path === "/strategies" ? <StrategiesPage dashboard={dashboard} operations={operations} onSelectStrategy={selectStrategy} />
              : currentRoute.path === "/data" ? <DataPage operations={operations} dashboard={dashboard} />
                : currentRoute.path === "/risk" ? <RiskPage operations={operations} system={system} onSetManualHalt={setManualHalt} />
                  : <AccountPage
                    operations={operations}
                    system={system}
                    ledger={ledger}
                    onSync={syncAccount}
                    onRecordFill={recordStrategyFill}
                    onConfirmDividend={confirmDividend}
                  />
        )}
      </main>
    </div>
  );
}

export default App;
