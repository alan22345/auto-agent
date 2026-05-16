async function fetchRepos(): Promise<unknown> {
  const r = await fetch("/api/repos");
  return r.json();
}

async function createRepo(payload: object): Promise<unknown> {
  const r = await axios.post("/api/repos", payload);
  return r.data;
}

async function getRepo(): Promise<unknown> {
  const r = await fetch("/api/repos/42");
  return r.json();
}
