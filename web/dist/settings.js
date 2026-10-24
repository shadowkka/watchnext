// Queue limits belong to the server; only connection/display preferences are local.
export const limitKeys = ['episodeLimit', 'reserveGb', 'capGb'];
export function serverLimits(status) {
  return {episodeLimit: status.episodeCountLimit ?? null, reserveGb: status.reserveGb, capGb: status.capGb ?? null};
}
export function localSettings(settings) {
  return {server: settings.server, demo: settings.demo, pollSeconds: settings.pollSeconds};
}
export function limitPatch(draft, dirty) {
  const names = {episodeLimit:'episodeCountLimit', reserveGb:'reserveGb', capGb:'capGb'};
  return Object.fromEntries(limitKeys.filter(key => dirty.has(key)).map(key => [names[key], draft[key]]));
}
export function synchronizeLimits(settings, draft, status, dirty) {
  const limits = serverLimits(status);
  Object.assign(settings, limits);
  for (const key of limitKeys) if (!dirty.has(key)) draft[key] = limits[key];
}
