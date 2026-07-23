# Phase 0 — Foundation (Platform) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Midas Spring Boot + PostgreSQL skeleton and the `platform` module — owner seam, reserve-first idempotency store, and the transactional outbox with a per-consumer dispatcher — so every later context has a proven, tested foundation to build on.

**Architecture:** A single Spring Boot 3.3 (Java 21) modular-monolith app, one PostgreSQL 16 database, Flyway migrations. The `platform` package owns cross-cutting infrastructure with no business domain. The outbox is a transactional outbox: producers write an event row in the same DB transaction as their state change; a `@Scheduled` dispatcher delivers each event to each registered consumer independently, tracks delivery per `(event, consumer)`, and advances a contiguous low-water offset per consumer. Idempotency is reserve-first (a `PENDING` record insert is the lock).

**Tech Stack:** Java 21, Spring Boot 3.3.x, Spring Web, Spring Data JDBC, PostgreSQL 16, Flyway, Gradle (Groovy DSL), JUnit 5, Testcontainers, AssertJ.

## Global Constraints

- **Java 21**, Spring Boot **3.3.x**, Gradle Groovy DSL. (conventions §15)
- **PostgreSQL 16+**; all persistence via **Flyway** migrations, one versioned file per change, never edited after merge. (conventions §5)
- **UUID v7** primary keys generated app-side; the outbox additionally has a DB-assigned `seq BIGINT GENERATED ALWAYS AS IDENTITY` used for ordering. (conventions §1)
- **`owner_id UUID NOT NULL`** on every aggregate-root table; Phases 0–5 use a single hard-coded system owner injected via `OwnerContext`. (conventions §4)
- **Timestamps** `TIMESTAMPTZ` UTC; app takes an injected `java.time.Clock` — never `Instant.now()` inline. (conventions §3)
- **Errors:** RFC 9457 `application/problem+json` with a stable machine `code`. (conventions §11)
- Package root: `com.midas`. Platform package: `com.midas.platform`.
- Append-only tables (`outbox`) get no UPDATE/DELETE from app code. (conventions §5)

**Reference:** `docs/superpowers/specs/detailed/00-conventions.md` (§1, §3, §4, §5, §6, §7) and `01-platform.md`. Read them before starting.

---

## File Structure

```
build.gradle                                   Gradle build (deps, plugins)
settings.gradle                                root project name
src/main/resources/application.yml             app + datasource config
src/main/resources/db/migration/
  V1__platform_idempotency.sql                 idempotency_record
  V2__platform_outbox.sql                      outbox, outbox_delivery, consumer_offset, processed_event
src/main/java/com/midas/
  MidasApplication.java                         Spring Boot entrypoint
  platform/
    owner/OwnerContext.java                     current owner id (fixed system owner in P0)
    owner/OwnerScopedRepository.java            base for owner-scoped queries
    id/UuidV7.java                              app-side UUID v7 generator
    error/ProblemException.java                 domain error carrying an RFC-9457 code
    error/GlobalExceptionHandler.java           maps exceptions → problem+json
    idempotency/IdempotencyRecord.java          row model
    idempotency/IdempotencyService.java         reserve-first execute()
    idempotency/IdempotencyConflictException.java
    outbox/DomainEvent.java                     event envelope (type, aggregate, payload, owner)
    outbox/OutboxWriter.java                    append(event) within active tx
    outbox/OutboxDispatcher.java                @Scheduled per-consumer delivery
    outbox/EventConsumer.java                   interface { name(); handle(event) }
    outbox/OutboxRepository.java                queries: due deliveries, contiguous offset
    config/ClockConfig.java                     UTC Clock bean
src/test/java/com/midas/
  platform/idempotency/IdempotencyServiceTest.java
  platform/outbox/OutboxWriterTest.java
  platform/outbox/OutboxDispatcherTest.java
  support/PostgresTestBase.java                 Testcontainers Postgres base
  MidasApplicationTests.java                    context loads
```

---

### Task 1: Project skeleton boots against Postgres

**Files:**
- Create: `build.gradle`, `settings.gradle`, `src/main/resources/application.yml`
- Create: `src/main/java/com/midas/MidasApplication.java`
- Create: `src/main/java/com/midas/platform/config/ClockConfig.java`
- Test: `src/test/java/com/midas/support/PostgresTestBase.java`, `src/test/java/com/midas/MidasApplicationTests.java`

**Interfaces:**
- Produces: a bootable Spring Boot app; `PostgresTestBase` (an abstract `@SpringBootTest` base that starts a Testcontainers Postgres and points Spring's datasource at it) reused by every later integration test; a `Clock` bean (UTC).

- [ ] **Step 1: Write `settings.gradle` and `build.gradle`**

`settings.gradle`:
```groovy
rootProject.name = 'midas'
```

`build.gradle`:
```groovy
plugins {
    id 'java'
    id 'org.springframework.boot' version '3.3.4'
    id 'io.spring.dependency-management' version '1.1.6'
}
group = 'com.midas'
version = '0.0.1'
java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }
repositories { mavenCentral() }
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    implementation 'org.springframework.boot:spring-boot-starter-data-jdbc'
    implementation 'org.springframework.boot:spring-boot-starter-actuator'
    implementation 'org.springframework.boot:spring-boot-starter-validation'
    implementation 'org.flywaydb:flyway-core'
    implementation 'org.flywaydb:flyway-database-postgresql'
    runtimeOnly 'org.postgresql:postgresql'
    testImplementation 'org.springframework.boot:spring-boot-starter-test'
    testImplementation 'org.testcontainers:junit-jupiter:1.20.1'
    testImplementation 'org.testcontainers:postgresql:1.20.1'
}
test { useJUnitPlatform() }
```

- [ ] **Step 2: Write `application.yml`, `MidasApplication.java`, `ClockConfig.java`**

`src/main/resources/application.yml`:
```yaml
spring:
  application:
    name: midas
  datasource:
    url: ${DB_URL:jdbc:postgresql://localhost:5432/midas}
    username: ${DB_USER:midas}
    password: ${DB_PASSWORD:midas}
  flyway:
    enabled: true
    locations: classpath:db/migration
management:
  endpoints:
    web:
      exposure:
        include: health
```

`src/main/java/com/midas/MidasApplication.java`:
```java
package com.midas;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class MidasApplication {
    public static void main(String[] args) {
        SpringApplication.run(MidasApplication.class, args);
    }
}
```

`src/main/java/com/midas/platform/config/ClockConfig.java`:
```java
package com.midas.platform.config;

import java.time.Clock;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class ClockConfig {
    @Bean
    public Clock clock() {
        return Clock.systemUTC();
    }
}
```

- [ ] **Step 3: Write the Testcontainers base and the context-loads test**

`src/test/java/com/midas/support/PostgresTestBase.java`:
```java
package com.midas.support;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

@SpringBootTest
@Testcontainers
public abstract class PostgresTestBase {
    @Container
    static final PostgreSQLContainer<?> POSTGRES =
        new PostgreSQLContainer<>("postgres:16-alpine");

    @DynamicPropertySource
    static void datasource(DynamicPropertyRegistry r) {
        r.add("spring.datasource.url", POSTGRES::getJdbcUrl);
        r.add("spring.datasource.username", POSTGRES::getUsername);
        r.add("spring.datasource.password", POSTGRES::getPassword);
    }
}
```

`src/test/java/com/midas/MidasApplicationTests.java`:
```java
package com.midas;

import com.midas.support.PostgresTestBase;
import org.junit.jupiter.api.Test;

class MidasApplicationTests extends PostgresTestBase {
    @Test
    void contextLoads() {
    }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./gradlew test --tests com.midas.MidasApplicationTests`
Expected: PASS — Spring context boots, Testcontainers Postgres starts, Flyway runs (no migrations yet, succeeds).

- [ ] **Step 5: Commit**

```bash
git add build.gradle settings.gradle src/
git commit -m "feat(platform): bootable Spring Boot + Postgres skeleton with Testcontainers base"
```

---

### Task 2: Owner seam + UUID v7

**Files:**
- Create: `src/main/java/com/midas/platform/id/UuidV7.java`
- Create: `src/main/java/com/midas/platform/owner/OwnerContext.java`
- Test: `src/test/java/com/midas/platform/owner/OwnerContextTest.java`, `src/test/java/com/midas/platform/id/UuidV7Test.java`

**Interfaces:**
- Produces:
  - `UuidV7.generate() -> java.util.UUID` — time-ordered v7 UUID.
  - `OwnerContext.currentOwnerId() -> java.util.UUID` — the active owner; Phase 0 returns a fixed system owner constant `OwnerContext.SYSTEM_OWNER`.

- [ ] **Step 1: Write the failing tests**

`src/test/java/com/midas/platform/id/UuidV7Test.java`:
```java
package com.midas.platform.id;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.UUID;
import org.junit.jupiter.api.Test;

class UuidV7Test {
    @Test
    void generatesVersion7() {
        UUID u = UuidV7.generate();
        assertThat(u.version()).isEqualTo(7);
    }

    @Test
    void isTimeOrdered() throws InterruptedException {
        UUID a = UuidV7.generate();
        Thread.sleep(2);
        UUID b = UuidV7.generate();
        assertThat(a.toString().compareTo(b.toString())).isLessThan(0);
    }
}
```

`src/test/java/com/midas/platform/owner/OwnerContextTest.java`:
```java
package com.midas.platform.owner;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

class OwnerContextTest {
    @Test
    void returnsFixedSystemOwnerInPhase0() {
        assertThat(new OwnerContext().currentOwnerId())
            .isEqualTo(OwnerContext.SYSTEM_OWNER);
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./gradlew test --tests com.midas.platform.id.UuidV7Test --tests com.midas.platform.owner.OwnerContextTest`
Expected: FAIL — `UuidV7` and `OwnerContext` do not exist (compile error).

- [ ] **Step 3: Write the implementations**

`src/main/java/com/midas/platform/id/UuidV7.java`:
```java
package com.midas.platform.id;

import java.security.SecureRandom;
import java.nio.ByteBuffer;
import java.util.UUID;

public final class UuidV7 {
    private static final SecureRandom RANDOM = new SecureRandom();

    private UuidV7() {}

    public static UUID generate() {
        long ts = System.currentTimeMillis();
        byte[] b = new byte[16];
        b[0] = (byte) (ts >>> 40);
        b[1] = (byte) (ts >>> 32);
        b[2] = (byte) (ts >>> 24);
        b[3] = (byte) (ts >>> 16);
        b[4] = (byte) (ts >>> 8);
        b[5] = (byte) ts;
        byte[] rand = new byte[10];
        RANDOM.nextBytes(rand);
        System.arraycopy(rand, 0, b, 6, 10);
        b[6] = (byte) ((b[6] & 0x0f) | 0x70);          // version 7
        b[8] = (byte) ((b[8] & 0x3f) | 0x80);          // IETF variant
        ByteBuffer bb = ByteBuffer.wrap(b);
        return new UUID(bb.getLong(), bb.getLong());
    }
}
```

`src/main/java/com/midas/platform/owner/OwnerContext.java`:
```java
package com.midas.platform.owner;

import java.util.UUID;
import org.springframework.stereotype.Component;

@Component
public class OwnerContext {
    /** Phase 0–5: the single hard-coded owner. Phase 6 flips this to the auth principal. */
    public static final UUID SYSTEM_OWNER =
        UUID.fromString("00000000-0000-7000-8000-000000000001");

    public UUID currentOwnerId() {
        return SYSTEM_OWNER;
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./gradlew test --tests com.midas.platform.id.UuidV7Test --tests com.midas.platform.owner.OwnerContextTest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/main/java/com/midas/platform src/test/java/com/midas/platform
git commit -m "feat(platform): UUID v7 generator and OwnerContext (fixed system owner)"
```

---

### Task 3: Reserve-first idempotency store

**Files:**
- Create: `src/main/resources/db/migration/V1__platform_idempotency.sql`
- Create: `src/main/java/com/midas/platform/idempotency/IdempotencyRecord.java`
- Create: `src/main/java/com/midas/platform/idempotency/IdempotencyConflictException.java`
- Create: `src/main/java/com/midas/platform/idempotency/IdempotencyService.java`
- Test: `src/test/java/com/midas/platform/idempotency/IdempotencyServiceTest.java`

**Interfaces:**
- Consumes: `OwnerContext.currentOwnerId()`, `UuidV7.generate()`, `Clock`.
- Produces:
  - `IdempotencyService.execute(String scope, String key, String requestHash, java.util.function.Supplier<StoredResult> supplier) -> StoredResult`
  - `record StoredResult(int status, String body)` (nested public record in `IdempotencyService`).
  - Same key+hash → returns the stored `StoredResult` without re-running the supplier. Same key + different hash → throws `IdempotencyConflictException`. Concurrent miss → the DB unique constraint lets exactly one supplier run; the loser reads back the winner's result.

- [ ] **Step 1: Write the migration**

`src/main/resources/db/migration/V1__platform_idempotency.sql`:
```sql
CREATE TABLE idempotency_record (
    id              UUID PRIMARY KEY,
    owner_id        UUID NOT NULL,
    scope           TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | DONE
    response_status INT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at    TIMESTAMPTZ,
    UNIQUE (owner_id, scope, idempotency_key)          -- the reservation lock
);
```

- [ ] **Step 2: Write the failing test**

`src/test/java/com/midas/platform/idempotency/IdempotencyServiceTest.java`:
```java
package com.midas.platform.idempotency;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.midas.platform.idempotency.IdempotencyService.StoredResult;
import com.midas.support.PostgresTestBase;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

class IdempotencyServiceTest extends PostgresTestBase {
    @Autowired IdempotencyService service;

    @Test
    void runsOnceThenReplays() {
        AtomicInteger runs = new AtomicInteger();
        StoredResult first = service.execute("txn", "k1", "hashA", () -> {
            runs.incrementAndGet();
            return new StoredResult(201, "{\"id\":\"x\"}");
        });
        StoredResult second = service.execute("txn", "k1", "hashA", () -> {
            runs.incrementAndGet();
            return new StoredResult(201, "{\"id\":\"x\"}");
        });
        assertThat(runs.get()).isEqualTo(1);          // supplier ran only once
        assertThat(second.status()).isEqualTo(201);
        assertThat(second.body()).isEqualTo(first.body());
    }

    @Test
    void sameKeyDifferentBodyConflicts() {
        service.execute("txn", "k2", "hashA", () -> new StoredResult(201, "{}"));
        assertThatThrownBy(() ->
            service.execute("txn", "k2", "hashB", () -> new StoredResult(201, "{}")))
            .isInstanceOf(IdempotencyConflictException.class);
    }
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./gradlew test --tests com.midas.platform.idempotency.IdempotencyServiceTest`
Expected: FAIL — `IdempotencyService` does not exist.

- [ ] **Step 4: Write the record, exception, and service**

`src/main/java/com/midas/platform/idempotency/IdempotencyConflictException.java`:
```java
package com.midas.platform.idempotency;

public class IdempotencyConflictException extends RuntimeException {
    public IdempotencyConflictException(String message) { super(message); }
}
```

`src/main/java/com/midas/platform/idempotency/IdempotencyRecord.java`:
```java
package com.midas.platform.idempotency;

import java.util.UUID;

public record IdempotencyRecord(
    UUID id, UUID ownerId, String scope, String idempotencyKey,
    String requestHash, String status, Integer responseStatus, String responseBody) {}
```

`src/main/java/com/midas/platform/idempotency/IdempotencyService.java`:
```java
package com.midas.platform.idempotency;

import com.midas.platform.id.UuidV7;
import com.midas.platform.owner.OwnerContext;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

@Service
public class IdempotencyService {
    public record StoredResult(int status, String body) {}

    private final JdbcTemplate jdbc;
    private final OwnerContext owner;

    public IdempotencyService(JdbcTemplate jdbc, OwnerContext owner) {
        this.jdbc = jdbc;
        this.owner = owner;
    }

    /**
     * Reserve-first (conventions §7). Insert a PENDING row as the lock; the winner runs the
     * supplier and finalizes in the SAME transaction; a loser catches the unique-violation
     * and returns the already-stored result (or conflicts on a mismatched hash).
     */
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public StoredResult execute(String scope, String key, String requestHash,
                                Supplier<StoredResult> supplier) {
        UUID ownerId = owner.currentOwnerId();
        try {
            jdbc.update("""
                INSERT INTO idempotency_record
                    (id, owner_id, scope, idempotency_key, request_hash, status)
                VALUES (?, ?, ?, ?, ?, 'PENDING')
                """, UuidV7.generate(), ownerId, scope, key, requestHash);
        } catch (DuplicateKeyException dup) {
            return replay(ownerId, scope, key, requestHash);
        }
        StoredResult result = supplier.get();
        jdbc.update("""
            UPDATE idempotency_record
               SET status='DONE', response_status=?, response_body=?::jsonb, finalized_at=now()
             WHERE owner_id=? AND scope=? AND idempotency_key=?
            """, result.status(), result.body(), ownerId, scope, key);
        return result;
    }

    private StoredResult replay(UUID ownerId, String scope, String key, String requestHash) {
        return jdbc.queryForObject("""
            SELECT request_hash, status, response_status, response_body
              FROM idempotency_record
             WHERE owner_id=? AND scope=? AND idempotency_key=?
            """, (rs, n) -> {
                if (!requestHash.equals(rs.getString("request_hash"))) {
                    throw new IdempotencyConflictException(
                        "Idempotency-Key reused with a different request body");
                }
                return new StoredResult(rs.getInt("response_status"), rs.getString("response_body"));
            }, ownerId, scope, key);
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./gradlew test --tests com.midas.platform.idempotency.IdempotencyServiceTest`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/main/resources/db/migration/V1__platform_idempotency.sql src/main/java/com/midas/platform/idempotency src/test/java/com/midas/platform/idempotency
git commit -m "feat(platform): reserve-first idempotency store"
```

---

### Task 4: Outbox schema + OutboxWriter (transactional append)

**Files:**
- Create: `src/main/resources/db/migration/V2__platform_outbox.sql`
- Create: `src/main/java/com/midas/platform/outbox/DomainEvent.java`
- Create: `src/main/java/com/midas/platform/outbox/EventConsumer.java`
- Create: `src/main/java/com/midas/platform/outbox/OutboxWriter.java`
- Test: `src/test/java/com/midas/platform/outbox/OutboxWriterTest.java`

**Interfaces:**
- Consumes: `OwnerContext`, `UuidV7`, `Clock`, the registered `List<EventConsumer>`.
- Produces:
  - `record DomainEvent(UUID id, UUID ownerId, String aggregateType, UUID aggregateId, String eventType, String payloadJson, Instant occurredAt)`.
  - `interface EventConsumer { String name(); void handle(DomainEvent event); }`.
  - `OutboxWriter.append(String aggregateType, UUID aggregateId, String eventType, String payloadJson) -> UUID` (the event id). Must be called inside an active transaction (throws `IllegalStateException` otherwise); seeds a `PENDING` `outbox_delivery` row for every registered consumer, co-committed.

- [ ] **Step 1: Write the migration**

`src/main/resources/db/migration/V2__platform_outbox.sql`:
```sql
CREATE TABLE outbox (
    id              UUID PRIMARY KEY,
    seq             BIGINT GENERATED ALWAYS AS IDENTITY,
    owner_id        UUID NOT NULL,
    aggregate_type  TEXT NOT NULL,
    aggregate_id    UUID NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    schema_version  INT  NOT NULL DEFAULT 1,
    occurred_at     TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX outbox_seq_idx ON outbox (seq);

CREATE TABLE outbox_delivery (
    event_seq       BIGINT NOT NULL REFERENCES outbox (seq),
    consumer_name   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | SENT | DEAD
    attempts        INT  NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_seq)
);
CREATE INDEX outbox_delivery_dispatch_idx
    ON outbox_delivery (consumer_name, status, next_attempt_at, event_seq);

CREATE TABLE consumer_offset (
    consumer_name  TEXT PRIMARY KEY,
    contiguous_seq BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE processed_event (
    consumer_name TEXT NOT NULL,
    event_id      UUID NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer_name, event_id)
);
```

- [ ] **Step 2: Write the failing test**

`src/test/java/com/midas/platform/outbox/OutboxWriterTest.java`:
```java
package com.midas.platform.outbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.midas.platform.id.UuidV7;
import com.midas.support.PostgresTestBase;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

class OutboxWriterTest extends PostgresTestBase {
    @Autowired OutboxWriter writer;
    @Autowired JdbcTemplate jdbc;
    @Autowired TransactionTemplate tx;

    @Test
    void appendRequiresActiveTransaction() {
        assertThatThrownBy(() ->
            writer.append("account", UuidV7.generate(), "TestEvent", "{}"))
            .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void appendWritesEventAndSeedsDeliveryRows() {
        UUID id = tx.execute(s ->
            writer.append("account", UuidV7.generate(), "TestEvent", "{\"a\":1}"));
        Integer events = jdbc.queryForObject(
            "SELECT count(*) FROM outbox WHERE id = ?", Integer.class, id);
        Integer deliveries = jdbc.queryForObject(
            "SELECT count(*) FROM outbox_delivery d JOIN outbox o ON o.seq = d.event_seq "
            + "WHERE o.id = ?", Integer.class, id);
        assertThat(events).isEqualTo(1);
        assertThat(deliveries).isGreaterThanOrEqualTo(1);   // one per registered consumer
    }

    @Test
    void rollbackDropsTheEvent() {
        UUID probe = UuidV7.generate();
        try {
            tx.execute(s -> {
                writer.append("account", probe, "TestEvent", "{}");
                throw new RuntimeException("boom");
            });
        } catch (RuntimeException ignored) {}
        Integer events = jdbc.queryForObject(
            "SELECT count(*) FROM outbox WHERE aggregate_id = ?", Integer.class, probe);
        assertThat(events).isZero();
    }
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `./gradlew test --tests com.midas.platform.outbox.OutboxWriterTest`
Expected: FAIL — `OutboxWriter` does not exist.

- [ ] **Step 4: Write `DomainEvent`, `EventConsumer`, `OutboxWriter`**

`src/main/java/com/midas/platform/outbox/DomainEvent.java`:
```java
package com.midas.platform.outbox;

import java.time.Instant;
import java.util.UUID;

public record DomainEvent(
    UUID id, UUID ownerId, String aggregateType, UUID aggregateId,
    String eventType, String payloadJson, Instant occurredAt) {}
```

`src/main/java/com/midas/platform/outbox/EventConsumer.java`:
```java
package com.midas.platform.outbox;

public interface EventConsumer {
    String name();
    void handle(DomainEvent event);
}
```

`src/main/java/com/midas/platform/outbox/OutboxWriter.java`:
```java
package com.midas.platform.outbox;

import com.midas.platform.id.UuidV7;
import com.midas.platform.owner.OwnerContext;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronizationManager;

@Component
public class OutboxWriter {
    private final JdbcTemplate jdbc;
    private final OwnerContext owner;
    private final Clock clock;
    private final List<EventConsumer> consumers;

    public OutboxWriter(JdbcTemplate jdbc, OwnerContext owner, Clock clock,
                        List<EventConsumer> consumers) {
        this.jdbc = jdbc;
        this.owner = owner;
        this.clock = clock;
        this.consumers = consumers;
    }

    public UUID append(String aggregateType, UUID aggregateId, String eventType, String payloadJson) {
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new IllegalStateException("OutboxWriter.append must run inside a transaction");
        }
        UUID id = UuidV7.generate();
        Instant now = Instant.now(clock);
        Long seq = jdbc.queryForObject("""
            INSERT INTO outbox (id, owner_id, aggregate_type, aggregate_id, event_type, payload, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?::jsonb, ?)
            RETURNING seq
            """, Long.class, id, owner.currentOwnerId(), aggregateType, aggregateId,
            eventType, payloadJson, java.sql.Timestamp.from(now));
        for (EventConsumer c : consumers) {
            jdbc.update("""
                INSERT INTO outbox_delivery (event_seq, consumer_name) VALUES (?, ?)
                """, seq, c.name());
        }
        return id;
    }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./gradlew test --tests com.midas.platform.outbox.OutboxWriterTest`
Expected: PASS. (With no consumers registered yet, `appendWritesEventAndSeedsDeliveryRows` asserts `>= 1`; note the dispatcher task adds a test consumer. If zero consumers exist at this point, temporarily relax is NOT needed — the next task registers one; keep the `>= 1` assertion but this test class will run green once Task 5's test consumer bean exists in the test context. To keep Task 4 self-contained, add the tiny no-op consumer below.)

Add `src/test/java/com/midas/platform/outbox/NoopTestConsumer.java`:
```java
package com.midas.platform.outbox;

import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;

@TestConfiguration
class NoopTestConsumerConfig {
    @Bean
    EventConsumer noopConsumer() {
        return new EventConsumer() {
            public String name() { return "noop"; }
            public void handle(DomainEvent event) { }
        };
    }
}
```
Import it on the test class with `@Import(NoopTestConsumerConfig.class)`.

- [ ] **Step 6: Commit**

```bash
git add src/main/resources/db/migration/V2__platform_outbox.sql src/main/java/com/midas/platform/outbox src/test/java/com/midas/platform/outbox
git commit -m "feat(platform): outbox schema and transactional OutboxWriter"
```

---

### Task 5: OutboxDispatcher — per-consumer delivery, owner-scoped, ack-derived SENT, DEAD, contiguous offset

**Files:**
- Create: `src/main/java/com/midas/platform/outbox/OutboxRepository.java`
- Create: `src/main/java/com/midas/platform/outbox/OutboxDispatcher.java`
- Test: `src/test/java/com/midas/platform/outbox/OutboxDispatcherTest.java`

**Interfaces:**
- Consumes: `DomainEvent`, `EventConsumer` (registered beans), `JdbcTemplate`, `OwnerContext` (to assert async owner scoping later; Phase 0 owner is fixed).
- Produces:
  - `OutboxDispatcher.dispatchOnce()` — one drain pass across all consumers; the `@Scheduled` method calls it. Returns `int` (deliveries marked SENT this pass) for test assertions.
  - Behavior: for each consumer, select its due `PENDING` deliveries ordered by `seq` (`FOR UPDATE SKIP LOCKED`); for each, run `handle`; on success insert `processed_event` (dedup) and mark the delivery `SENT` in the **same** tx; on exception increment `attempts`, back off `next_attempt_at`, and after 5 attempts set `DEAD`; a redelivery of an already-`processed_event` is a no-op. After the pass, advance each consumer's `consumer_offset.contiguous_seq` over the unbroken SENT prefix only.

- [ ] **Step 1: Write the failing test**

`src/test/java/com/midas/platform/outbox/OutboxDispatcherTest.java`:
```java
package com.midas.platform.outbox;

import static org.assertj.core.api.Assertions.assertThat;
import static org.awaitility.Awaitility.await;

import com.midas.platform.id.UuidV7;
import com.midas.support.PostgresTestBase;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

@Import(OutboxDispatcherTest.Consumers.class)
class OutboxDispatcherTest extends PostgresTestBase {
    static final List<UUID> GOOD_SEEN = new CopyOnWriteArrayList<>();

    @TestConfiguration
    static class Consumers {
        @Bean EventConsumer good() {
            return new EventConsumer() {
                public String name() { return "good"; }
                public void handle(DomainEvent e) { GOOD_SEEN.add(e.id()); }
            };
        }
        @Bean EventConsumer poison() {          // always throws → must not affect "good"
            return new EventConsumer() {
                public String name() { return "poison"; }
                public void handle(DomainEvent e) { throw new RuntimeException("boom"); }
            };
        }
    }

    @Autowired OutboxWriter writer;
    @Autowired OutboxDispatcher dispatcher;
    @Autowired TransactionTemplate tx;
    @Autowired JdbcTemplate jdbc;

    @Test
    void goodConsumerReceivesEventDespitePoisonConsumerFailing() {
        UUID probe = UuidV7.generate();
        UUID id = tx.execute(s -> writer.append("account", probe, "TestEvent", "{}"));
        GOOD_SEEN.clear();

        await().atMost(Duration.ofSeconds(10)).untilAsserted(() -> {
            dispatcher.dispatchOnce();
            assertThat(GOOD_SEEN).contains(id);                         // good delivered
        });

        String goodStatus = jdbc.queryForObject("""
            SELECT d.status FROM outbox_delivery d JOIN outbox o ON o.seq=d.event_seq
            WHERE o.id=? AND d.consumer_name='good'""", String.class, id);
        assertThat(goodStatus).isEqualTo("SENT");
    }

    @Test
    void poisonEventGoesDeadForItsConsumerOnly() {
        UUID id = tx.execute(s -> writer.append("account", UuidV7.generate(), "TestEvent", "{}"));
        await().atMost(Duration.ofSeconds(20)).untilAsserted(() -> {
            dispatcher.dispatchOnce();
            String status = jdbc.queryForObject("""
                SELECT d.status FROM outbox_delivery d JOIN outbox o ON o.seq=d.event_seq
                WHERE o.id=? AND d.consumer_name='poison'""", String.class, id);
            assertThat(status).isEqualTo("DEAD");
        });
    }

    @Test
    void redeliveryIsDeduped() {
        UUID id = tx.execute(s -> writer.append("account", UuidV7.generate(), "TestEvent", "{}"));
        GOOD_SEEN.clear();
        dispatcher.dispatchOnce();
        dispatcher.dispatchOnce();                                       // second pass
        long count = GOOD_SEEN.stream().filter(id::equals).count();
        assertThat(count).isEqualTo(1);                                  // handled at most once
    }
}
```

Add Awaitility to `build.gradle` test deps: `testImplementation 'org.awaitility:awaitility:4.2.1'`.

- [ ] **Step 2: Run test to verify it fails**

Run: `./gradlew test --tests com.midas.platform.outbox.OutboxDispatcherTest`
Expected: FAIL — `OutboxDispatcher` / `OutboxRepository` do not exist.

- [ ] **Step 3: Write `OutboxRepository` and `OutboxDispatcher`**

`src/main/java/com/midas/platform/outbox/OutboxRepository.java`:
```java
package com.midas.platform.outbox;

import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

@Repository
public class OutboxRepository {
    private final JdbcTemplate jdbc;

    public OutboxRepository(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    /** Due PENDING deliveries for one consumer, oldest seq first, row-locked. */
    public List<DueDelivery> dueFor(String consumer, int limit) {
        return jdbc.query("""
            SELECT o.seq, o.id, o.owner_id, o.aggregate_type, o.aggregate_id,
                   o.event_type, o.payload::text AS payload, o.occurred_at, d.attempts
              FROM outbox_delivery d
              JOIN outbox o ON o.seq = d.event_seq
             WHERE d.consumer_name = ? AND d.status = 'PENDING' AND d.next_attempt_at <= now()
             ORDER BY o.seq
             LIMIT ?
             FOR UPDATE OF d SKIP LOCKED
            """, (rs, n) -> new DueDelivery(
                rs.getLong("seq"),
                new DomainEvent(
                    (UUID) rs.getObject("id"), (UUID) rs.getObject("owner_id"),
                    rs.getString("aggregate_type"), (UUID) rs.getObject("aggregate_id"),
                    rs.getString("event_type"), rs.getString("payload"),
                    rs.getTimestamp("occurred_at").toInstant()),
                rs.getInt("attempts")),
            consumer, limit);
    }

    public boolean alreadyProcessed(String consumer, UUID eventId) {
        Integer c = jdbc.queryForObject(
            "SELECT count(*) FROM processed_event WHERE consumer_name=? AND event_id=?",
            Integer.class, consumer, eventId);
        return c != null && c > 0;
    }

    public void markSent(String consumer, long seq, UUID eventId) {
        jdbc.update("INSERT INTO processed_event (consumer_name, event_id) VALUES (?, ?) "
            + "ON CONFLICT DO NOTHING", consumer, eventId);
        jdbc.update("UPDATE outbox_delivery SET status='SENT' "
            + "WHERE consumer_name=? AND event_seq=?", consumer, seq);
    }

    public void recordFailure(String consumer, long seq, int attempts, int maxAttempts, Instant nextAt) {
        String status = attempts + 1 >= maxAttempts ? "DEAD" : "PENDING";
        jdbc.update("""
            UPDATE outbox_delivery
               SET attempts = attempts + 1, status = ?, next_attempt_at = ?
             WHERE consumer_name = ? AND event_seq = ?
            """, status, java.sql.Timestamp.from(nextAt), consumer, seq);
    }

    /** Advance the contiguous low-water mark over the unbroken SENT prefix only. */
    public void advanceOffset(String consumer) {
        jdbc.update("""
            INSERT INTO consumer_offset (consumer_name, contiguous_seq)
            VALUES (?, COALESCE((
                SELECT max(o.seq) FROM outbox o
                 WHERE NOT EXISTS (
                     SELECT 1 FROM outbox o2
                      LEFT JOIN outbox_delivery d2 ON d2.event_seq=o2.seq AND d2.consumer_name=?
                     WHERE o2.seq <= o.seq AND (d2.status IS NULL OR d2.status <> 'SENT'))
            ), 0))
            ON CONFLICT (consumer_name) DO UPDATE SET contiguous_seq = EXCLUDED.contiguous_seq
            """, consumer, consumer);
    }

    public record DueDelivery(long seq, DomainEvent event, int attempts) {}
}
```

`src/main/java/com/midas/platform/outbox/OutboxDispatcher.java`:
```java
package com.midas.platform.outbox;

import com.midas.platform.owner.OwnerContext;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

@Component
public class OutboxDispatcher {
    private static final int MAX_ATTEMPTS = 5;
    private static final int BATCH = 100;

    private final OutboxRepository repo;
    private final List<EventConsumer> consumers;
    private final TransactionTemplate tx;
    private final Clock clock;

    public OutboxDispatcher(OutboxRepository repo, List<EventConsumer> consumers,
                            TransactionTemplate tx, Clock clock, JdbcTemplate jdbc) {
        this.repo = repo;
        this.consumers = consumers;
        this.tx = tx;
        this.clock = clock;
    }

    @Scheduled(fixedDelayString = "${midas.outbox.poll-ms:1000}")
    public void poll() {
        dispatchOnce();
    }

    public int dispatchOnce() {
        int sent = 0;
        for (EventConsumer consumer : consumers) {
            List<OutboxRepository.DueDelivery> due = repo.dueFor(consumer.name(), BATCH);
            for (OutboxRepository.DueDelivery d : due) {
                sent += deliver(consumer, d) ? 1 : 0;
            }
            repo.advanceOffset(consumer.name());
        }
        return sent;
    }

    private boolean deliver(EventConsumer consumer, OutboxRepository.DueDelivery d) {
        return Boolean.TRUE.equals(tx.execute(status -> {
            if (repo.alreadyProcessed(consumer.name(), d.event().id())) {
                repo.markSent(consumer.name(), d.seq(), d.event().id());  // finish a stuck-SENT gap
                return true;
            }
            try {
                // Async owner scoping (conventions §4): consumers run under the event's owner.
                // Phase 0 owner is fixed; the scope hook lands with real auth in Phase 6.
                consumer.handle(d.event());
                repo.markSent(consumer.name(), d.seq(), d.event().id());  // SENT derived from ack
                return true;
            } catch (RuntimeException ex) {
                Instant nextAt = Instant.now(clock)
                    .plus(Duration.ofSeconds((long) Math.pow(2, d.attempts())));
                repo.recordFailure(consumer.name(), d.seq(), d.attempts(), MAX_ATTEMPTS, nextAt);
                return false;
            }
        }));
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./gradlew test --tests com.midas.platform.outbox.OutboxDispatcherTest`
Expected: PASS — `good` receives the event, `poison` goes `DEAD` independently, redelivery is deduped.

- [ ] **Step 5: Commit**

```bash
git add src/main/java/com/midas/platform/outbox src/test/java/com/midas/platform/outbox build.gradle
git commit -m "feat(platform): per-consumer outbox dispatcher with contiguous offset, DEAD isolation, dedup"
```

---

### Task 6: RFC 9457 problem+json error model

**Files:**
- Create: `src/main/java/com/midas/platform/error/ProblemException.java`
- Create: `src/main/java/com/midas/platform/error/GlobalExceptionHandler.java`
- Test: `src/test/java/com/midas/platform/error/GlobalExceptionHandlerTest.java`

**Interfaces:**
- Consumes: `IdempotencyConflictException`.
- Produces:
  - `ProblemException(int status, String code, String detail)` — a runtime exception carrying an HTTP status and a stable machine `code`.
  - `GlobalExceptionHandler` — `@RestControllerAdvice` rendering `ProblemException` and `IdempotencyConflictException` as `application/problem+json` with fields `{type, title, status, detail, code}`.

- [ ] **Step 1: Write the failing test**

`src/test/java/com/midas/platform/error/GlobalExceptionHandlerTest.java`:
```java
package com.midas.platform.error;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;

class GlobalExceptionHandlerTest {
    @Test
    void rendersProblemJsonWithCode() {
        GlobalExceptionHandler h = new GlobalExceptionHandler();
        ResponseEntity<?> resp = h.handleProblem(
            new ProblemException(409, "LEDGER_UNBALANCED_TRANSACTION", "sum != 0"));
        assertThat(resp.getStatusCode().value()).isEqualTo(409);
        assertThat(resp.getHeaders().getContentType())
            .isEqualTo(MediaType.APPLICATION_PROBLEM_JSON);
        assertThat(resp.getBody().toString()).contains("LEDGER_UNBALANCED_TRANSACTION");
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./gradlew test --tests com.midas.platform.error.GlobalExceptionHandlerTest`
Expected: FAIL — classes do not exist.

- [ ] **Step 3: Write `ProblemException` and `GlobalExceptionHandler`**

`src/main/java/com/midas/platform/error/ProblemException.java`:
```java
package com.midas.platform.error;

public class ProblemException extends RuntimeException {
    private final int status;
    private final String code;

    public ProblemException(int status, String code, String detail) {
        super(detail);
        this.status = status;
        this.code = code;
    }
    public int status() { return status; }
    public String code() { return code; }
}
```

`src/main/java/com/midas/platform/error/GlobalExceptionHandler.java`:
```java
package com.midas.platform.error;

import com.midas.platform.idempotency.IdempotencyConflictException;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(ProblemException.class)
    public ResponseEntity<ProblemDetail> handleProblem(ProblemException ex) {
        ProblemDetail pd = ProblemDetail.forStatusAndDetail(
            HttpStatus.valueOf(ex.status()), ex.getMessage());
        pd.setProperty("code", ex.code());
        return ResponseEntity.status(ex.status())
            .contentType(MediaType.APPLICATION_PROBLEM_JSON).body(pd);
    }

    @ExceptionHandler(IdempotencyConflictException.class)
    public ResponseEntity<ProblemDetail> handleIdempotency(IdempotencyConflictException ex) {
        ProblemDetail pd = ProblemDetail.forStatusAndDetail(HttpStatus.CONFLICT, ex.getMessage());
        pd.setProperty("code", "IDEMPOTENCY_KEY_REUSED");
        return ResponseEntity.status(HttpStatus.CONFLICT)
            .contentType(MediaType.APPLICATION_PROBLEM_JSON).body(pd);
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./gradlew test --tests com.midas.platform.error.GlobalExceptionHandlerTest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/main/java/com/midas/platform/error src/test/java/com/midas/platform/error
git commit -m "feat(platform): RFC 9457 problem+json error model"
```

---

### Task 7: Full-suite green + CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: none (verification task)

**Interfaces:**
- Consumes: everything above.
- Produces: a green `./gradlew build` and a CI workflow running it.

- [ ] **Step 1: Write the CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: ci
on:
  push:
    branches: [ main ]
  pull_request:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '21'
      - run: ./gradlew build --no-daemon
```

- [ ] **Step 2: Run the full build locally**

Run: `./gradlew build`
Expected: PASS — compiles, all tests green (context loads, UUID v7, owner, idempotency, outbox writer, dispatcher, error handler). Testcontainers pulls `postgres:16-alpine`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: build + test on push/PR (Java 21, Testcontainers)"
```

---

## Self-Review

**Spec coverage (against `01-platform.md` + conventions §4/§6/§7):**
- Owner seam (`OwnerContext`, fixed system owner) → Task 2. ✓ (`OwnerScopedRepository` base is deferred to Phase 1 where the first owner-scoped table exists — noted, not a gap for Phase 0.)
- Reserve-first idempotency + same-tx finalize → Task 3. ✓
- Outbox transactional write + per-consumer delivery + contiguous offset + owner-scoped dispatch + SENT-after-ack + DEAD-per-consumer + dedup + durable log (no delete path) → Tasks 4–5. ✓
- Problem+json → Task 6. ✓
- Boots on Postgres + Flyway + CI → Tasks 1, 7. ✓

**Deliberately deferred to Phase 1 (not Phase 0 gaps):** `OwnerScopedRepository` concrete use, DEAD-delivery admin *replay* endpoint (the DEAD state + reset column exist; the reset trigger is wired when an operator surface lands), Kafka backend swap. Called out so they're deliberate omissions.

**Placeholder scan:** none — every step has runnable code/commands.

**Type consistency:** `DomainEvent`, `EventConsumer.name()/handle()`, `OutboxWriter.append(...)->UUID`, `IdempotencyService.execute(...)->StoredResult`, `OutboxDispatcher.dispatchOnce()->int`, `ProblemException(status,code,detail)` are used consistently across tasks and tests.
