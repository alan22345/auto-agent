/**
 * Simple calculator module.
 */

function add(a, b) {
  return a + b;
}

function subtract(a, b) {
  return a - b;
}

function multiply(a, b) {
  return a * b;
}

function divide(a, b) {
  if (b === 0) {
    throw new Error("Division by zero");
  }
  return a / b;
}

function power(base, exponent) {
  return Math.pow(base, exponent);
}

function factorial(n) {
  if (n < 0) throw new Error("Negative numbers not supported");
  if (n === 0 || n === 1) return 1;
  return n * factorial(n - 1);
}

module.exports = { add, subtract, multiply, divide, power, factorial };
