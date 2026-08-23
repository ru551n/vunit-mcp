library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rtl;

library vunit_lib;
context vunit_lib.vunit_context;

entity tb_counter is
  generic (
    runner_cfg : string
  );
end entity tb_counter;

architecture test of tb_counter is
  constant clk_period : time := 10 ns;
  signal clk   : std_logic := '0';
  signal reset : std_logic := '1';
  signal inc   : std_logic := '0';
  signal count : std_logic_vector(7 downto 0);
begin
  clk <= not clk after clk_period / 2;

  dut : entity rtl.counter
    generic map (width => 8)
    port map (
      clk   => clk,
      reset => reset,
      inc   => inc,
      count => count
    );

  test_runner : process
  begin
    test_runner_setup(runner, runner_cfg);

    reset <= '1';
    inc <= '0';
    wait for 2 * clk_period;
    reset <= '0';

    while test_suite loop
      if run("counts up when inc asserted") then
        inc <= '1';
        wait for 3 * clk_period;
        inc <= '0';
        check_equal(to_integer(unsigned(count)), 3);

      elsif run("resets to zero") then
        inc <= '1';
        wait for 5 * clk_period;
        inc <= '0';
        reset <= '1';
        wait for clk_period;
        reset <= '0';
        check_equal(to_integer(unsigned(count)), 0);
      end if;
    end loop;

    test_runner_cleanup(runner);
  end process;
end architecture test;
