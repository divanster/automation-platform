// UI hints derived from the permissions in GET /api/auth/me/. The backend re-checks every call; these only decide what to
// show and which calls are worth making.

export const VIEW_OWN_PERMISSION = "intake.view_own_request";
export const VIEW_ANY_PERMISSION = "intake.view_any_request";

/** May the user list requests at all? Without either permission the list endpoint answers 403. */
export function canViewRequests(can: (permission: string) => boolean): boolean {
  return can(VIEW_OWN_PERMISSION) || can(VIEW_ANY_PERMISSION);
}

/** Does the list cover everybody's requests (true) or only the user's own (false)? A hint for wording, never for counting. */
export function seesAllRequests(can: (permission: string) => boolean): boolean {
  return can(VIEW_ANY_PERMISSION);
}
