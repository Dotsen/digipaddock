// A question form v1 on the page: the answers a person builds, what is still required, and what an
// answered form shows. The server checks the same rules (kd.cabinet.questions); here they only guide the person.
import type { FormAnswer, FormAnswerItem, QuestionFormV1 } from "./types";

export const SHORT_MAX = 3; // a round of up to three questions is one list; a longer one goes step by step

export function emptyAnswers(form: QuestionFormV1): Record<string, FormAnswerItem> {
  return Object.fromEntries(form.questions.map((q) => [q.id, { selected_option_ids: [], custom_text: "" }]));
}

export function answered(item: FormAnswerItem | undefined): boolean {
  return !!item && (item.selected_option_ids.length > 0 || item.custom_text.trim() !== "");
}

/** The required questions without an answer, in the form's order. */
export function missingRequired(form: QuestionFormV1, answers: Record<string, FormAnswerItem>): string[] {
  return form.questions.filter((q) => q.required && !answered(answers[q.id])).map((q) => q.id);
}

/** Every question of the form with the labels chosen (in the options' order) and the person's own words. */
export function summary(
  form: QuestionFormV1,
  answer: Pick<FormAnswer, "answers">,
): { title: string; labels: string[]; custom: string }[] {
  return form.questions.map((q) => {
    const item = answer.answers[q.id];
    const chosen = new Set(item?.selected_option_ids ?? []);
    return {
      title: q.title,
      labels: q.options.filter((o) => chosen.has(o.id)).map((o) => o.label),
      custom: item?.custom_text ?? "",
    };
  });
}

export function isShort(form: QuestionFormV1): boolean {
  return form.questions.length <= SHORT_MAX;
}
