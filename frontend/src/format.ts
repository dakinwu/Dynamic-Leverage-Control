const money = new Intl.NumberFormat("zh-TW", {
  style: "currency",
  currency: "TWD",
  maximumFractionDigits: 0,
});

export function formatMoney(value: string | null): string {
  return value === null ? "未知" : money.format(Number(value));
}

export function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatPercent(value: string | null): string {
  return value === null ? "未知" : `${(Number(value) * 100).toFixed(2)}%`;
}

/** Total of decimal-string amounts, e.g. dividend receivables keyed by action id. */
export function sumAmounts(amounts: Record<string, string>): number {
  return Object.values(amounts).reduce((total, amount) => total + Number(amount), 0);
}

export function allocationForExposure(target: number | null): { base: number; leveraged: number; cash: number } {
  if (target === null || !Number.isFinite(target)) return { base: 0, leveraged: 0, cash: 0 };
  const clipped = Math.max(0, Math.min(2, target));
  if (clipped <= 1) return { base: clipped, leveraged: 0, cash: 1 - clipped };
  return { base: 2 - clipped, leveraged: clipped - 1, cash: 0 };
}
