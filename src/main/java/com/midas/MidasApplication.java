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
