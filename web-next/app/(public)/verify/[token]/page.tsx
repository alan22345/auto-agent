'use client';
import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { Card } from '@/components/ui/card';
import { verifyEmail } from '@/lib/secrets';

export default function VerifyPage() {
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const token = params?.token;
    if (!token) return;
    verifyEmail(token)
      .then(() => {
        setDone(true);
        // Tiny delay so the user sees the confirmation before redirect.
        setTimeout(() => router.push('/tasks'), 800);
      })
      .catch((err: Error) => setError(err.message || 'Verification failed'));
  }, [params, router]);

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6 space-y-3">
        {error ? (
          <>
            <h1 className="text-xl font-semibold text-destructive">
              Verification failed
            </h1>
            <p className="text-sm">{error}</p>
            <Link href="/signup" className="text-sm text-primary underline">
              Sign up again
            </Link>
          </>
        ) : done ? (
          <>
            <h1 className="text-xl font-semibold text-primary">Verified</h1>
            <p className="text-sm">Redirecting to your tasks…</p>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Verifying your email…</p>
        )}
      </Card>
    </main>
  );
}
