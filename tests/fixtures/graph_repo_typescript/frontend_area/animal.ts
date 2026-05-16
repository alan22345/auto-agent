export class Animal {
  name: string;
  constructor(name: string) {
    this.name = name;
  }
  speak(): string {
    return "...";
  }
}

export class Dog extends Animal {
  bark(): string {
    return this.speak();
  }
}

export function helper(): number {
  return 1;
}

export const VERSION = "1.0.0";

export type Pair<A, B> = [A, B];
