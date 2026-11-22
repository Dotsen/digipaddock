// The TOTP side of a step-up: `confirmed(purpose, run)` is `withStepUp` whose code comes from the StepUp
// dialog (components/StepUp.svelte, mounted once by the app). A person with a passkey never sees it.
import { withStepUp, type Proof } from "./stepup";

class StepUpPrompt {
  purpose = $state("");
  open = $state(false);
  private resolve: ((code: string | null) => void) | null = null;

  /** Show the dialog for `purpose`; the code, or null when it is cancelled. An earlier prompt is cancelled. */
  ask(purpose: string): Promise<string | null> {
    this.answer(null);
    this.purpose = purpose;
    this.open = true;
    return new Promise((resolve) => (this.resolve = resolve));
  }

  answer(code: string | null) {
    const resolve = this.resolve;
    this.resolve = null;
    this.open = false;
    resolve?.(code);
  }
}

export const stepUpPrompt = new StepUpPrompt();

export const askTotp = (purpose: string) => () => stepUpPrompt.ask(purpose);

export function confirmed<T>(purpose: string, run: (proof?: Proof) => Promise<T>): Promise<T> {
  return withStepUp(purpose, run, askTotp(purpose));
}
