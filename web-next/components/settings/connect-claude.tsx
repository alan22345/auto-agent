'use client';
import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useClaudePairing } from '@/hooks/useClaudePairing';
import { disconnectClaude, getPairStatus } from '@/lib/claude-pairing';

export default function ConnectClaude() {
  const qc = useQueryClient();
  const { data: status } = useQuery({
    queryKey: ['claude-pair-status'],
    queryFn: getPairStatus,
    refetchInterval: 5000,
  });

  const { state, begin, submit } = useClaudePairing();
  const [code, setCode] = useState('');

  const disconnect = useMutation({
    mutationFn: disconnectClaude,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['claude-pair-status'] });
      qc.invalidateQueries({ queryKey: ['auth', 'me'] });
    },
  });

  useEffect(() => {
    if (state.phase === 'done') {
      qc.invalidateQueries({ queryKey: ['claude-pair-status'] });
      qc.invalidateQueries({ queryKey: ['auth', 'me'] });
    }
  }, [state.phase, qc]);

  if (status?.claude_auth_status === 'paired') {
    return (
      <div className="space-y-4">
        <div className="text-green-700">
          Connected
          {status.claude_paired_at
            ? ` (since ${new Date(status.claude_paired_at).toLocaleString()})`
            : ''}
          .
        </div>
        <Button variant="outline" onClick={() => disconnect.mutate()}>
          Disconnect
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {status?.claude_auth_status === 'expired' && (
        <div className="text-amber-700 text-sm">
          Your previous Claude session expired. Reconnect to resume queued tasks.
        </div>
      )}

      {state.phase === 'idle' && <Button onClick={begin}>Connect Claude</Button>}

      {state.phase === 'starting' && <div>Starting pairing session…</div>}
      {state.phase === 'awaiting_url' && <div>Waiting for login URL…</div>}

      {state.phase === 'awaiting_code' && state.url && (
        <div className="space-y-3">
          <p className="text-sm">Open this link in a new tab and complete the login:</p>
          <a
            href={state.url}
            target="_blank"
            rel="noreferrer"
            className="text-blue-600 underline break-all"
          >
            {state.url}
          </a>
          <p className="text-sm">Then paste the one-time code below:</p>
          <Input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="paste code"
          />
          <Button disabled={!code} onClick={() => submit(code)}>
            Submit
          </Button>
        </div>
      )}

      {state.phase === 'submitting' && <div>Verifying…</div>}
      {state.phase === 'done' && <div className="text-green-700">Connected.</div>}
      {state.phase === 'error' && (
        <div className="text-red-700 text-sm">{state.error}</div>
      )}
    </div>
  );
}
