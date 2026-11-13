export const routes = [
  { path: "/", label: "總覽", short: "總" },
  { path: "/strategies", label: "策略", short: "策" },
  { path: "/data", label: "資料", short: "資" },
  { path: "/risk", label: "風控", short: "風" },
  { path: "/account", label: "帳務", short: "帳" },
] as const;

export type Route = (typeof routes)[number];

export const strategyCatalog = [
  { version: "constant-1.2-v1", label: "固定 1.2x", mode: "paper" },
  { version: "constant-2.0-v1", label: "固定 2.0x", mode: "paper" },
  { version: "dvl-tw-v2-shadow", label: "DVL-TW v2（Shadow）", mode: "shadow" },
] as const;

export function routeFor(pathname: string): Route {
  return routes.find((route) => route.path === pathname) ?? routes[0];
}
