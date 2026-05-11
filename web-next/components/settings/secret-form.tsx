'use client';
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  clearSecret,
  listSecrets,
  setSecret,
  testSecret,
  type SecretKey,
} from '@/lib/secrets';

interface Props {
  secretKey: SecretKey;
  label: string;
  placeholder: string;
  helpText?: string;
}

/** Shared form for any per-user secret. The value field is treated as
 *  write-only — the server never returns it. */
export default function SecretForm({ secretKey, label, placeholder, helpText }: Props) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['my-secrets'],
    queryFn: listSecrets,
  });
  const isSet = data?.keys.includes(secretKey) ?? false;

  const [value, setValue] = useState('');
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);

  const save = useMutation({
    mutationFn: () => setSecret(secretKey, value),
    onSuccess: () => {
      setValue('');
      setTestResult(null);
      qc.invalidateQueries({ queryKey: ['my-secrets'] });
    },
  });

  const clear = useMutation({
    mutationFn: () => clearSecret(secretKey),
    onSuccess: () => {
      setTestResult(null);
      qc.invalidateQueries({ queryKey: ['my-secrets'] });
    },
  });

  const test = useMutation({
    mutationFn: () => testSecret(secretKey),
    onSuccess: (res) => setTestResult(res),
  });

  return (
    <div className="space-y-3">
      <div className="text-sm">
        Status:{' '}
        {isLoading ? (
          <span className="text-muted-foreground">checking…</span>
        ) : isSet ? (
          <span className="text-green-700">stored on the server</span>
        ) : (
          <span className="text-muted-foreground">not set</span>
        )}
      </div>

      <div>
        <label className="text-sm font-medium">{label}</label>
        <Input
          type="password"
          value={value}
          placeholder={isSet ? '•••••••• (re-enter to replace)' : placeholder}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
        />
        {helpText && (
          <p className="mt-1 text-xs text-muted-foreground">{helpText}</p>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <Button onClick={() => save.mutate()} disabled={!value || save.isPending}>
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
        <Button
          variant="outline"
          onClick={() => test.mutate()}
          disabled={!isSet || test.isPending}
        >
          {test.isPending ? 'Testing…' : 'Test connection'}
        </Button>
        <Button
          variant="ghost"
          onClick={() => clear.mutate()}
          disabled={!isSet || clear.isPending}
        >
          Clear
        </Button>
      </div>

      {save.error && (
        <p className="text-sm text-destructive">
          {(save.error as Error).message}
        </p>
      )}
      {testResult && (
        <p
          className={`text-sm ${
            testResult.ok ? 'text-green-700' : 'text-destructive'
          }`}
        >
          {testResult.ok ? '✓ ' : '✗ '}
          {testResult.detail}
        </p>
      )}
    </div>
  );
}
