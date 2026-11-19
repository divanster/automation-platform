import { getRequest } from "../api/requests";
import type { RequestDetail } from "../api/types";
import { detailPollDelay } from "./liveIntervals";
import { useLiveResource } from "./useLiveResource";

/** One request's detail, kept fresh in the background (see `useLiveResource` and `detailPollDelay`). */
export function useRequestDetail(runId: string) {
  return useLiveResource<RequestDetail>((signal) => getRequest(runId, signal), [runId], {
    interval: (data, polls) => detailPollDelay(data.state, polls),
  });
}
