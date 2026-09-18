import 'package:flutter_test/flutter_test.dart';
import 'package:yggdrasil_console/features/status/domain/status.dart';
import 'package:yggdrasil_console/features/status/presentation/widgets/system_card.dart';

void main() {
  test('a healthy 2xx probe shows only its latency', () {
    expect(
      describeProbe(const Probe(healthy: true, statusCode: 200, latencyMs: 12)),
      '12 ms',
    );
  });

  test('a failing HTTP answer shows its status code', () {
    expect(
      describeProbe(const Probe(healthy: false, statusCode: 503, latencyMs: 8)),
      'HTTP 503 · 8 ms',
    );
    expect(
      describeProbe(const Probe(healthy: false, statusCode: 302, latencyMs: 3)),
      'HTTP 302 · 3 ms',
    );
  });

  test('a slow 2xx shows the latency in seconds', () {
    expect(
      describeProbe(
        const Probe(healthy: false, statusCode: 200, latencyMs: 2380),
      ),
      '2.4 s',
    );
  });

  test('no answer shows the error', () {
    expect(
      describeProbe(const Probe(healthy: false, error: 'connection refused')),
      'connection refused',
    );
    expect(
      describeProbe(
        const Probe(healthy: false, latencyMs: 5000, error: 'timeout'),
      ),
      '5.0 s · timeout',
    );
  });

  test('nothing else known falls back to healthy / unhealthy', () {
    expect(describeProbe(const Probe(healthy: true)), 'healthy');
    expect(describeProbe(const Probe()), 'unhealthy');
  });
}
