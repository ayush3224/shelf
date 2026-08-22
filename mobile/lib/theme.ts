/** One palette, committed. Light only — Phase 1 has nothing that fires at night. */
export const color = {
  bg: '#FBFAF8',
  surface: '#FFFFFF',
  text: '#1A1917',
  muted: '#78736B',
  faint: '#A9A399',
  border: '#E8E4DD',
  accent: '#2F6F4E',
  accentText: '#FFFFFF',
  overdue: '#B4441F',
  danger: '#B4441F',
} as const;

export const space = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 40,
} as const;

export const radius = {
  sm: 8,
  md: 12,
  pill: 999,
} as const;
