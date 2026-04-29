import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import './globals.css';
import { Providers } from '@/components/providers';

const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });
const jbMono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-jb-mono' });

export const metadata: Metadata = {
  title: 'Auto-Agent',
  description: 'Autonomous AI scrum team',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jbMono.variable}`}>
      <body><Providers>{children}</Providers></body>
    </html>
  );
}
