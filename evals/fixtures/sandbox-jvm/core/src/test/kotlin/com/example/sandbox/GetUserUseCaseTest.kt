package com.example.sandbox

import app.cash.turbine.test
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class GetUserUseCaseTest {

    private val repository: UserRepository = mockk()
    private val useCase = GetUserUseCase(repository)
    private val user = User(id = "42", name = "Ada", email = "ada@example.com")

    @Test
    fun `invoke returns the user from the repository`() = runTest {
        coEvery { repository.getUser("42") } returns user

        assertEquals(user, useCase("42"))
        coVerify(exactly = 1) { repository.getUser("42") }
    }

    @Test
    fun `observe emits the user and completes`() = runTest {
        coEvery { repository.getUser("42") } returns user

        useCase.observe("42").test {
            assertEquals(user, awaitItem())
            awaitComplete()
        }
    }
}
