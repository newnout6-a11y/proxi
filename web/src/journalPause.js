/** The pause of the journal list: the events as they were when the pause began, and how many came since.

The pause used to live in the shared journal stream: it froze the turn list and the error badge of every section, the
events kept piling up in memory with no bound, and running turns were shown as interrupted after 20 minutes. Now the
stream keeps flowing (capped as always) and only the list of events holds still. */
export function pausedView(live, frozen) {
  if (!frozen) return { events: live, pending: 0 };
  const last = frozen.length ? frozen[frozen.length - 1].seq : 0;
  let pending = 0;
  for (let index = live.length - 1; index >= 0 && live[index].seq > last; index -= 1) pending += 1;
  return { events: frozen, pending };
}
