// Question forms for the tests: a short round of three and a long one of four.
import type { FormQuestion, QuestionFormV1 } from "./types";

export const FORM: QuestionFormV1 = {
  schema_version: 1,
  text: "Уточнения",
  questions: [
    {
      id: "method",
      title: "Как проверять?",
      context: "Монитор на том же сервере.",
      type: "single_select",
      required: true,
      allow_custom: true,
      options: [
        { id: "http", label: "HTTP", description: "напрямую", recommended: true },
        { id: "ssh", label: "SSH", description: "через шлюз", recommended: false },
      ],
    },
    {
      id: "extra",
      title: "Что ещё?",
      context: "",
      type: "multi_select",
      required: false,
      allow_custom: true,
      options: [
        { id: "worker", label: "Обработчик", description: "", recommended: false },
        { id: "queue", label: "Очередь", description: "", recommended: false },
      ],
    },
    { id: "details", title: "Условия", context: "", type: "text", required: true, allow_custom: false, options: [] },
  ],
};

const FOURTH: FormQuestion = {
  id: "size",
  title: "Размер?",
  context: "",
  type: "single_select",
  required: false,
  allow_custom: false,
  options: [
    { id: "s", label: "S", description: "", recommended: false },
    { id: "m", label: "M", description: "", recommended: false },
  ],
};

export const LONG: QuestionFormV1 = { ...FORM, questions: [...FORM.questions, FOURTH] };
