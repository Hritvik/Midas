package com.midas;

import static org.assertj.core.api.Assertions.assertThat;

import com.midas.support.PostgresTestBase;
import java.time.Clock;
import java.time.ZoneOffset;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

class MidasApplicationTests extends PostgresTestBase {
    @Autowired
    private Clock clock;

    @Test
    void contextLoadsWithUtcClock() {
        assertThat(clock.getZone()).isEqualTo(ZoneOffset.UTC);
    }
}
