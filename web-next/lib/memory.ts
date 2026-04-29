export async function uploadMemoryFile(file: File): Promise<string> {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/memory/upload', {
    method: 'POST',
    body: fd,
    credentials: 'include',
  });
  if (!res.ok) throw new Error(`Upload failed: ${await res.text()}`);
  const json = await res.json();
  return json.source_id as string;
}
