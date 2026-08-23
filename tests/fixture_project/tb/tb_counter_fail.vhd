library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library rtl;

library vunit_lib;
context vunit_lib.vunit_context;

entity tb_counter_fail is
  generic (
    runner_cfg : string
  );
end entity tb_counter_fail;

architecture test of tb_counter_fail is
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
    wait for 2 * clk_period;
    reset <= '0';

    while test_suite loop
      if run("deliberately fails") then
        inc <= '1';
        wait for 3 * clk_period;
        inc <= '0';
        check_equal(to_integer(unsigned(count)), 99, "expected 99, counter only counts to 3");
      end if;
    end loop;

    test_runner_cleanup(runner);
  end process;
end architecture test;
