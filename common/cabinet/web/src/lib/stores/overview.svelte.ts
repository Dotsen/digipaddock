// The overview: one aggregate for the first screen, for the project and period of the address.
// Each request has its generation: the answer of an older one (another project, another period) is dropped.
import { apiGet } from "../api";
import type { Overview, OverviewPeriod } from "../types";

export interface OverviewQuery {
  project: string;
  period: OverviewPeriod;
}

export const PERIODS: OverviewPeriod[] = ["1d", "7d", "30d"];

class OverviewStore {
  data = $state<Overview | null>(null);
  private generation = 0;

  async load(query: OverviewQuery) {
    const generation = ++this.generation;
    const params = new URLSearchParams();
    if (query.project) params.set("project", query.project);
    params.set("period", query.period);
    const found = await apiGet<Overview>(`/api/dashboard?${params}`);
    if (generation === this.generation) this.data = found;
  }

  /** Sign-out: nothing of this person stays for the next one, not even a read on its way. */
  reset() {
    this.generation++;
    this.data = null;
  }
}

export const overview = new OverviewStore();
