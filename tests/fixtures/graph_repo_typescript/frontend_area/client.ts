import { Animal, Dog, helper } from "./animal";
import * as ns from "./animal";

async function fetchRepos(): Promise<unknown> {
  const r = await fetch("/api/repos");
  return r.json();
}

async function postItem(payload: object): Promise<unknown> {
  const r = await axios.post("/api/items", payload);
  return r.data;
}

function makeDog(): Dog {
  helper();
  return new Dog("rex");
}

function dynamic(name: string): unknown {
  const obj: Record<string, () => string> = {};
  return obj[name]();
}
