import type { SignIn } from "@/types/api";

/** What a sign-in is called in a list: a phone or client by the name it
 * sends, a person's browsers together, a machine client by its subject
 * when it sends no name. */
export function signInLabel(signIn: SignIn): string {
  if (signIn.kind === "browser") return "Web browsers";
  if (signIn.name) return signIn.name;
  return signIn.kind === "phone" ? "Phone" : signIn.subject;
}

/** What revoking it does, said to whoever is about to. */
export function revokeConsequence(signIn: SignIn): string {
  if (signIn.kind === "browser") {
    return "Every web browser of this person is signed out. Signing in again at the identity provider, with a password or passkey, lets them back in.";
  }
  if (signIn.kind === "phone") {
    return "This phone is signed out. Signing in again on it starts a new sign-in.";
  }
  return "This access key stops working for good. A replacement is a new machine user at the identity provider.";
}
