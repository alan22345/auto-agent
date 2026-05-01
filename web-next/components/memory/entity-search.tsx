'use client';
import { Input } from '@/components/ui/input';

interface Props {
  value: string;
  onChange: (q: string) => void;
}

export function EntitySearch({ value, onChange }: Props) {
  return (
    <Input
      placeholder="Search entities…"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
