import { screen } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/** Opens the account menu in the top bar (its button is named after the signed-in user). */
export async function openAccountMenu(user: UserEvent, username = "synthetic.operator") {
  const trigger = await screen.findByRole("button", { name: new RegExp(username) });
  if (trigger.getAttribute("aria-expanded") !== "true") await user.click(trigger);
  return trigger;
}

/** Signs out through the account menu. */
export async function signOut(user: UserEvent, username?: string) {
  await openAccountMenu(user, username);
  await user.click(await screen.findByRole("button", { name: "Sign out" }));
}
